// Claude Code's own test kit draws the mod's band and pane on every surface, with the engine's element rules (a prop
// a surface does not take fails it). Copied beside the plugin and run by tests/test_desktop_mod.py when claude is here.
import { expect, test } from 'claude-code/testing'

const NEEDS = [
  { kind: 'permission', key: 'permission:p1', agent: 'Luca', tool: 'Bash', text: 'git push', plan: false, choices: ['allow', 'deny'] },
  { kind: 'message', key: 'message:m1', agent: 'Maya', text: 'May I merge #A3?' },
]
const POLL = { project: 'Demo', needs: NEEDS, messages: [{ seq: 4, from: 'Maya', to: 'Owner', text: 'Done with #A3' }] }

function engine(on) {
  on('env.get', (_, e) => ({ value: e.name === 'HOME' ? '/home/x' : undefined }))
  on('fs.read', (_, e) => ({ value: e.path.endsWith('config.json') ? '{"port": 8765}' : 'tok\n' }))
  on('session.cwd', () => ({ value: '/home/x/code' }))
  on('http.fetch', (_, e) => ({ value: { status: 200, ok: true, headers: {}, text: e.url.includes('/api/poll') ? JSON.stringify(POLL) : '{}' } }))
  on('command.register', () => ({ value: { command: 'crewchat' } }))
  on('clock.every', () => ({ value: undefined }))
  on('ui.invalidate', () => ({ value: undefined }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
}

test('the band and the pane draw on every surface', async ($, on) => {
  engine(on)
  await $.session.start({ cwd: '/home/x/code', surface: 'desktop', isInteractive: true })
  for (const surface of ['terminal', 'desktop', 'vscode', 'mobile']) {
    const band = await $.ui.mount({ plugin: 'crewchat', surface, component: 'AbovePrompt', requestId: 'band',
      props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 100 } })
    expect((await band.find({ type: 'Text', text: /1 to approve · 1 question/ }))).toBeDefined()
    await band.unmount()
    const pane = await $.ui.mount({ plugin: 'crewchat', surface, component: 'Pane', requestId: 'crewchat',
      props: { title: 'crewchat', isFocused: false, bodyColumns: 40, placement: 'dock', scroll: { offset: 0, bodyRows: 20 }, view: {} } })
    expect(await pane.find({ key: 'yes:permission:p1' })).toBeDefined()
    expect(await pane.find({ key: 'no:permission:p1' })).toBeDefined()
    await pane.unmount()
  }
})
