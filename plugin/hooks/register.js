// crewchat inside Claude Code (roadmap W14): a band above the prompt that says what needs you, and a pane beside the
// chat with the project's latest messages and Approve and Deny for what waits on you.
//
// It reads only this machine's crewchat server (127.0.0.1), with the owner's token, as `crewchat` commands do: the
// token and the port are in crewchat's home (~/.crewchat, or CREWCHAT_HOME). Nothing leaves the machine. Plain
// JavaScript, no JSX, so `node --check` reads it; the elements are built with the global `h`.

const PANE = 'crewchat'
const EVERY_MS = 4000 // how often it asks the server; the page long-polls, a mod's timer is enough here
const KEEP = 40 // messages kept for the pane
// Which answer is Approve and which is Deny, from the item's own choices (the server's, as the page shows them): the
// narrowest yes (this task, not every task; allow, not always allow), and the no.
const YES = ['allow', 'allow_task', 'approve', 'post', 'allow_app', 'allow_site']
const NO = ['deny', 'decline', 'drop']
// What each kind of item counts as in the band: one, many, and the kinds (the server's NEED_KINDS). The last group
// counts a kind none of the others names, so an item of a kind added later still shows.
const GROUPS = [
  ['to approve', 'to approve', ['permission', 'browser', 'device', 'gchat', 'process', 'firstrun']],
  ['stuck agent', 'stuck agents', ['stuck']],
  ['stopped commit', 'stopped commits', ['lease']],
  ['blocked task', 'blocked tasks', ['blocked']],
  ['to review', 'to review', ['review']],
  ['question', 'questions', ['ask', 'message']],  // an overdue ask, or a message that asks the owner something
  ['task given back', 'tasks given back', ['reclaimed', 'given_back']],
  ['other item', 'other items', null],
]
const NAMED = new Set(GROUPS.flatMap(([, , kinds]) => kinds || []))

// chat: the project the server found for this session's folder (null until it answers); first: it is the first one.
let state = { status: 'quiet', note: '', project: '', chat: null, first: true, needs: [], messages: [], after: -1 }
let known = null // the server, read once a session (server); read again after a request to it fails
let drawn = '' // what the band and the pane last drew from (shown): drawn again only when it changes
let reading = null // the refresh under way, if any
let queued = null // the one refresh asked for while another runs: it reads again once that one is done

/** The Approve and Deny answers for an item, or null when it is answered on the page (a reply, a review). */
export function answers(item) {
  const choices = Array.isArray(item && item.choices) ? item.choices : []
  const yes = YES.find(c => choices.includes(c))
  const no = NO.find(c => choices.includes(c))
  return yes && no ? { yes, no } : null
}

/** "2 to approve · 1 message", or "Nothing needs you". */
export function countLine(needs) {
  const parts = []
  for (const [one, many, kinds] of GROUPS) {
    const n = needs.filter(i => (kinds ? kinds.includes(i.kind) : !NAMED.has(i.kind))).length
    if (n) parts.push(n + ' ' + (n === 1 ? one : many))
  }
  return parts.length ? parts.join(' · ') : 'Nothing needs you'
}

async function home($) {
  const set = await $.env.get('CREWCHAT_HOME')
  const user = (await $.env.get('HOME')) || (await $.env.get('USERPROFILE')) || ''
  if (set) return set.startsWith('~') ? user + set.slice(1) : set
  return user ? user + '/.crewchat' : ''
}

/** {base, token, folder} for this session, or null when crewchat is not set up on this machine. Kept for the session:
 * a request that fails (forget) has it read again, as crewchat may have moved to another port or token. */
async function server($) {
  if (!known) known = await readServer($)
  return known
}

function forget() {
  known = null
}

async function readServer($) {
  const root = await home($)
  if (!root) return null
  let config, token
  try {
    config = JSON.parse(await $.fs.read(root + '/config.json'))
    token = (await $.fs.read(root + '/tokens/Owner.token')).trim()
  } catch (e) {
    return null
  }
  if (!token) return null
  const port = Number(config.port) || 8765
  return { base: 'http://127.0.0.1:' + port, token, folder: (await $.session.cwd()) || '' }
}

function headers(where, json) {
  const out = { Authorization: 'Bearer ' + where.token }
  // The session's folder: the server finds its project as `crewchat` commands do (the path resolved, so a symlink or
  // another spelling of it still finds it; a folder below a connected one too). Encoded: a header takes ASCII only.
  if (where.folder) out['X-Crewchat-Folder'] = encodeURIComponent(where.folder)
  if (json) out['Content-Type'] = 'application/json'
  return out
}

function said(text) {
  try {
    return JSON.parse(text).error || ''
  } catch (e) {
    return ''
  }
}

async function post($, where, path, body) {
  let res
  try {
    res = await $.http.fetch(where.base + path, { method: 'POST', headers: headers(where, true), body: JSON.stringify(body) })
  } catch (e) {
    forget()
    throw e
  }
  if (!res.ok) {
    forget()
    throw new Error(said(res.text) || 'the server answered ' + res.status)
  }
  return res.text ? JSON.parse(res.text) : {}
}

/** Asks the server what changed: the messages after the last one seen, and the whole of Needs you. One at a time: one
 * asked for while another runs (the timer, and a button's redraw) waits for it and then reads again, so two never
 * read from the same point and add the same messages twice. */
export function refresh($) {
  if (!reading) {
    reading = read($).finally(() => {
      reading = null
    })
    return reading
  }
  if (!queued) {
    queued = reading.catch(() => {}).then(() => {
      queued = null
      return refresh($)
    })
  }
  return queued
}

/** The server's light poll (what this mod shows, and it does not count as an open chat page, which would stop Web
 * Push): its answer, or a note on why there is none. */
async function poll($, where, after) {
  let res
  try {
    res = await $.http.fetch(where.base + '/api/poll?lite=1&after=' + after, { headers: headers(where) })
  } catch (e) {
    forget()
    return { note: 'crewchat is not running here. Start it with `crewchat serve`.' }
  }
  if (!res.ok) {
    forget()
    return { note: 'crewchat said: ' + (said(res.text) || 'HTTP ' + res.status) }
  }
  return { data: JSON.parse(res.text) }
}

async function read($) {
  const where = await server($)
  if (!where) {
    state = { ...state, status: 'quiet' }
    return state
  }
  let got = await poll($, where, state.after)
  if (got.data && got.data.chat !== state.chat && state.after !== -1) got = await poll($, where, -1) // another project now: from its latest
  if (!got.data) {
    state = { ...state, status: 'down', note: got.note }
    return state
  }
  const data = got.data
  const kept = data.chat === state.chat ? state.messages : []
  const seen = kept.length ? kept[kept.length - 1].seq : -1
  const messages = kept.concat((data.messages || []).filter(m => m.seq > seen)).slice(-KEEP)
  const last = messages.length ? messages[messages.length - 1].seq : -1
  state = { status: 'up', note: '', project: data.project || '', chat: data.chat, first: data.first !== false, needs: data.needs || [], messages, after: last }
  return state
}

/** What the band and the pane draw from, as one string to compare. */
const shown = () => JSON.stringify([state.status, state.note, state.project, state.needs, state.messages])

async function redraw($) {
  await refresh($)
  const now = shown()
  if (now === drawn) return // nothing new: the band and the pane stay as they are
  drawn = now
  $.ui.invalidate('ui.render')
}

async function answer($, item, choice, word) {
  const where = await server($)
  if (!where) return
  try {
    await post($, where, '/api/needs', item.kind === 'process' ? { key: item.key, answer: choice, of: item.of } : { key: item.key, answer: choice })
    $.ui.toast(word + ': ' + (item.agent || 'crewchat'))
  } catch (e) {
    $.ui.toast('Not answered: ' + e.message)
  }
  await redraw($)
}

/** Opens the chat page in the browser, signed in with a fresh one-time code, as `crewchat ui` does. */
async function openPage($) {
  const where = await server($)
  if (!where) return
  try {
    const { code } = await post($, where, '/api/login-code', {})
    const url = where.base + '/login?code=' + code + (state.chat && !state.first ? '&chat=' + encodeURIComponent(state.chat) : '')
    const windows = (await $.env.get('OS')) === 'Windows_NT'
    const mac = !windows && (await $.process.run(['uname'])).stdout.trim() === 'Darwin'
    const argv = windows ? ['rundll32', 'url.dll,FileProtocolHandler', url] : [mac ? 'open' : 'xdg-open', url]
    const ran = await $.process.run(argv)
    if (ran.exitCode !== 0) throw new Error('no browser opened')
  } catch (e) {
    $.ui.toast('Run `crewchat ui` to open the chat page (' + e.message + ')')
  }
}

function cut(text, room) {
  const flat = String(text || '').replace(/\s+/g, ' ').trim()
  return flat.length > room ? flat.slice(0, Math.max(1, room - 1)) + '…' : flat
}

function what(item) {
  if (item.kind === 'permission') return (item.plan ? 'plan' : item.tool) + ': ' + item.text
  if (item.kind === 'process') return 'process note: ' + item.title
  return (item.title ? '#' + item.task + ' ' + item.title + (item.text ? ': ' : '') : '') + (item.text || '')
}

export const register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'crewchat', description: "Show crewchat's chat and what needs you in a pane" })
    await refresh($)
    drawn = shown() // the first draw is the engine's own
    $.clock.every(EVERY_MS, () => redraw($))
    return next(e)
  })

  on('command.run', { command: 'crewchat' }, async $ => {
    await redraw($)
    await $.ui.open({ id: PANE, title: 'crewchat' })
    return { text: 'crewchat opened beside the chat.' }
  })

  on('ui.render', { component: 'AbovePrompt' }, ($, e, next) => {
    if (e.props.hasSurvey || state.status === 'quiet') return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const room = Math.max(20, (e.props.bodyColumns || 80) - 40)
    if (state.status === 'down') {
      return h(Box, { flexDirection: 'row', gap: 1 }, h(Text, { dimColor: true, wrap: 'truncate' }, 'crewchat · ' + state.note))
    }
    const last = state.messages[state.messages.length - 1]
    return h(
      Box,
      { flexDirection: 'row', gap: 1, alignItems: 'center' },
      h(Text, state.needs.length ? { bold: true, color: 'warning' } : { bold: true }, 'crewchat · ' + countLine(state.needs)),
      last ? h(Text, { dimColor: true, wrap: 'truncate' }, cut(last.from + ': ' + last.text, room)) : null,
      h(Button, { key: 'pane', label: 'Chat', onPress: () => $.ui.open({ id: PANE, title: 'crewchat' }) }),
      h(Button, { key: 'page', label: 'Open page', onPress: () => openPage($) }),
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const room = Math.max(20, (e.props.bodyColumns || 40) * 2)
    if (state.status !== 'up') {
      return h(Box, { flexDirection: 'column' }, h(Text, { dimColor: true }, state.note || 'crewchat is not set up on this machine.'))
    }
    const rows = []
    rows.push(h(Text, { bold: true }, (state.project || 'crewchat') + ' · ' + countLine(state.needs)))
    for (const item of state.needs.slice(0, 8)) {
      const pair = answers(item)
      rows.push(
        h(
          Box,
          { flexDirection: 'column', marginTop: 1 },
          // A command to approve is shown whole: what Approve lets it run (MAX_PERMISSION_TEXT on the server).
          h(Text, null, h(Text, { bold: true }, item.agent || item.from || 'crewchat'), ' ',
            cut(what(item), item.kind === 'permission' && !item.plan ? 2100 : room)),
          pair
            ? h(
                Box,
                { flexDirection: 'row', gap: 1 },
                h(Button, { key: 'yes:' + item.key, label: 'Approve', variant: 'primary', onPress: () => answer($, item, pair.yes, 'Approved') }),
                h(Button, { key: 'no:' + item.key, label: 'Deny', onPress: () => answer($, item, pair.no, 'Denied') }),
              )
            : null,
        ),
      )
    }
    const shown = Math.max(3, ((e.viewport && e.viewport.rows) || 24) - 6 - rows.length * 3)
    rows.push(h(Box, { marginTop: 1 }, h(Text, { dimColor: true }, 'Latest')))
    for (const m of state.messages.slice(-shown)) {
      rows.push(h(Text, null, h(Text, { bold: true }, m.from), ' → ' + m.to + ': ' + cut(m.text, room)))
    }
    if (!state.messages.length) rows.push(h(Text, { dimColor: true }, 'No messages yet.'))
    rows.push(h(Box, { flexDirection: 'row', gap: 1, marginTop: 1 }, h(Button, { key: 'open', label: 'Open page', onPress: () => openPage($) })))
    return h(Box, { flexDirection: 'column' }, ...rows)
  })
}
