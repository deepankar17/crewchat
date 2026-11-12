// Runs crewchat's Claude Code mod (plugin/hooks/register.js) outside Claude Code, for tests/test_desktop_mod.py: the
// engine's `$` stood in for by Node (files, environment, fetch), and the elements drawn as plain objects.
//
//   node desktop_mod_harness.mjs <register.js> <cwd> [press KEY | command | tick N | overlap TEXT]
//
// Starts the session, asks the server once, draws the band and the pane, presses KEY if given (and draws again), or
// runs the mod's timer N times, or (overlap) has the owner send TEXT and then runs the timer and /crewchat at once (and
// draws again), and prints {band, pane, after, toasts, fetched, ran, opened, commands, read,
// invalidated} as JSON. CREWCHAT_HOME and HOME come from the environment, as in a session.
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const [file, cwd, op, key] = process.argv.slice(2)
globalThis.h = (type, props, ...children) => ({ type, props: props || {}, children: children.flat().filter(c => c !== null && c !== undefined && c !== false) })

const toasts = [], fetched = [], ran = [], opened = [], commands = [], read = [], timers = []
let invalidated = 0
const elements = Object.fromEntries(['Box', 'Text', 'Button', 'Link', 'Markdown'].map(n => [n, n]))
const $ = {
  env: { get: async name => process.env[name] },
  fs: { read: async path => (read.push(path), readFileSync(path, 'utf8')) },
  session: { cwd: async () => cwd },
  http: {
    fetch: async (url, init = {}) => {
      fetched.push({ url, method: init.method || 'GET', headers: init.headers || {}, body: init.body || '' })
      const res = await fetch(url, init)
      return { status: res.status, ok: res.ok, headers: Object.fromEntries(res.headers), text: await res.text() }
    },
  },
  clock: { every: (ms, tick) => (timers.push(tick), { cancel() {} }) },
  command: { register: async c => commands.push(c.name) },
  process: { run: async argv => (ran.push(argv), { exitCode: 0, stdout: argv[0] === 'uname' ? 'Darwin\n' : '' }) },
  ui: {
    resolve: () => elements,
    invalidate: () => invalidated++,
    toast: text => toasts.push(text),
    open: async pane => (opened.push(pane.id), { isPlaced: true }),
  },
}

const hooks = []
const { register } = await import(pathToFileURL(file).href)
register((event, matcher, hook) => hooks.push(hook ? { event, matcher, hook } : { event, matcher: {}, hook: matcher }))

const fits = (m, e) => Object.entries(m).every(([k, v]) => (k === 'command' ? e.command === v : e[k] === v))
async function raise(event, e) {
  const hook = hooks.find(x => x.event === event && fits(x.matcher, e))
  return hook ? hook.hook($, e, async x => ({ passed: true, e: x })) : { passed: true }
}
const draw = async () => ({
  band: await raise('ui.render', { component: 'AbovePrompt', surface: 'desktop', props: { hasSurvey: false, bodyColumns: 100 } }),
  pane: await raise('ui.render', { component: 'Pane', requestId: 'crewchat', surface: 'desktop', props: { bodyColumns: 40 }, viewport: { rows: 40 } }),
})
function find(tree, k) {
  if (!tree || typeof tree !== 'object') return null
  if (tree.type === 'Button' && tree.props.key === k) return tree
  for (const c of tree.children || []) {
    const got = find(c, k)
    if (got) return got
  }
  return null
}

await raise('session.start', { cwd })
const first = await draw()
let after = null
if (op === 'press') {
  const button = find(first.pane, key) || find(first.band, key)
  if (!button) throw new Error('no button ' + key)
  await button.props.onPress()
  after = await draw()
} else if (op === 'command') {
  await raise('command.run', { command: 'crewchat' })
} else if (op === 'tick') {
  for (let i = 0; i < Number(key); i++) for (const tick of timers) await tick()
} else if (op === 'overlap') {
  const home = process.env.CREWCHAT_HOME
  const port = JSON.parse(readFileSync(home + '/config.json', 'utf8')).port
  const token = readFileSync(home + '/tokens/Owner.token', 'utf8').trim()
  await fetch('http://127.0.0.1:' + port + '/api/send', {
    method: 'POST',
    headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
    body: JSON.stringify({ to: 'all', text: key }),
  })
  await Promise.all([...timers.map(tick => tick()), raise('command.run', { command: 'crewchat' })])
  after = await draw()
}
process.stdout.write(JSON.stringify({ ...first, after, toasts, fetched, ran, opened, commands, read, invalidated }))
