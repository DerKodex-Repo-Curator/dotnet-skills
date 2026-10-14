import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { CopilotSdkExecutor } from '@microsoft/vally/executor';
import './sdk-startup.mjs';

async function fixture(t, { history, sendError } = {}) {
  const root = await mkdtemp(path.join(tmpdir(), 'vally-session-filesystem-'));
  const workDir = path.join(root, 'workspace');
  const logDir = path.join(root, 'logs');
  await mkdir(workDir);
  await writeFile(path.join(workDir, 'fixture.txt'), 'workspace sentinel', 'utf8');
  const calls = [];
  const configs = [];
  const histories = new Map();
  function session(sessionId, config) {
    configs.push(config);
    assert.equal(config.createSessionFsProvider, undefined, 'logging must not replace file tools');
    let listener;
    const events = [];
    histories.set(sessionId, events);
    return {
      sessionId,
      on(callback) { listener = callback; },
      async sendAndWait() {
        const content = await readFile(path.join(config.workingDirectory, 'fixture.txt'), 'utf8');
        const event = {
          id: randomUUID(),
          timestamp: new Date().toISOString(),
          type: 'assistant.message',
          data: { content },
        };
        events.push(event);
        listener(event);
        if (sendError) throw sendError;
        return event;
      },
      async getEvents() {
        calls.push(['events', sessionId]);
        return history ? history(events) : events;
      },
      async abort() { calls.push(['abort', sessionId]); },
      async disconnect() { calls.push(['disconnect', sessionId]); },
    };
  }
  const client = {
    async createSession(config) { return session(config.sessionId ?? randomUUID(), config); },
    async resumeSession(id, config) { return session(id, config); },
    async deleteSession(id) { calls.push(['delete', id]); },
    async stop() { return []; },
  };
  const executor = new CopilotSdkExecutor({
    createClient: () => client,
    createEnvClient: () => client,
    flushWindowMs: 0,
  });
  t.after(async () => {
    await executor.shutdown();
    await rm(root, { recursive: true, force: true });
  });
  const options = {
    workDir, timeout: 1000,
    sessionLog: { rootDir: logDir, sessionID: 'logged-session' },
  };
  const stimulus = { name: 'filesystem-probe', prompt: 'Handled by a fake session, not a model' };
  return { root, workDir, logDir, calls, configs, histories, executor, options, stimulus };
}

async function recordedEvents(logDir) {
  return (await readFile(path.join(logDir, 'events.jsonl'), 'utf8'))
    .trim().split('\n').map(line => JSON.parse(line));
}

test('shared and environment clients retain the SDK native filesystem', async t => {
  const executor = new CopilotSdkExecutor({ flushWindowMs: 0 });
  t.after(() => executor.shutdown());
  assert.equal(executor.getClient(true).sessionFsConfig, null);
  assert.equal(executor.admitEnvClient({ FILESYSTEM_PROBE: '1' }, true).sessionFsConfig, null);
});

for (const env of [undefined, { FILESYSTEM_PROBE: '1' }]) {
  test(`logged ${env ? 'environment' : 'shared'} sessions preserve workspace and native history`, async t => {
    const f = await fixture(t);
    const result = await f.executor.execute(f.stimulus, { ...f.options, env });
    assert.equal(result.output, 'workspace sentinel');
    assert.equal(f.configs[0].workingDirectory, f.workDir);
    assert.deepEqual(f.configs[0].infiniteSessions, { enabled: false });
    assert.equal(f.configs[0].largeOutput, undefined);
    assert.deepEqual(await recordedEvents(f.logDir), f.histories.get('logged-session'));
    assert.deepEqual(f.calls, [
      ['events', 'logged-session'], ['disconnect', 'logged-session'], ['delete', 'logged-session'],
    ]);
    assert.equal(await readFile(path.join(f.workDir, 'fixture.txt'), 'utf8'), 'workspace sentinel');
    assert.deepEqual(await readdir(f.logDir), ['events.jsonl']);
  });
}

test('resumed sessions export history but remain recoverable', async t => {
  const f = await fixture(t);
  await f.executor.execute(f.stimulus, { ...f.options, sessionID: 'resumed-session' });
  assert.deepEqual(await recordedEvents(f.logDir), f.histories.get('resumed-session'));
  assert.deepEqual(f.calls, [['events', 'resumed-session'], ['disconnect', 'resumed-session']]);
});

test('concurrent sessions do not share workspace or history', async t => {
  const f = await fixture(t);
  const secondWorkDir = path.join(f.root, 'second-workspace');
  const secondLogDir = path.join(f.root, 'second-logs');
  await mkdir(secondWorkDir);
  await writeFile(path.join(secondWorkDir, 'fixture.txt'), 'second sentinel', 'utf8');
  const results = await Promise.all([
    f.executor.execute(f.stimulus, f.options),
    f.executor.execute(f.stimulus, {
      ...f.options, workDir: secondWorkDir,
      sessionLog: { rootDir: secondLogDir, sessionID: 'second-session' },
    }),
  ]);
  assert.deepEqual(results.map(r => r.output), ['workspace sentinel', 'second sentinel']);
  assert.deepEqual(await recordedEvents(f.logDir), f.histories.get('logged-session'));
  assert.deepEqual(await recordedEvents(secondLogDir), f.histories.get('second-session'));
});

test('unlogged sessions do not export or delete SDK history', async t => {
  const f = await fixture(t);
  const { sessionLog, ...options } = f.options;
  await f.executor.execute(f.stimulus, options);
  assert.deepEqual(f.calls.map(([method]) => method), ['disconnect']);
});

test('failed history export reports fallback and preserves the SDK session', async t => {
  const warning = t.mock.method(console, 'warn', () => {});
  const f = await fixture(t, { history: () => { throw new Error('history unavailable'); } });
  await f.executor.execute(f.stimulus, f.options);
  assert.deepEqual(await readdir(f.logDir), []);
  assert.deepEqual(f.calls, [['events', 'logged-session'], ['disconnect', 'logged-session']]);
  assert.equal(warning.mock.callCount(), 1);
  assert.match(warning.mock.calls[0].arguments[0], /raw-event fallback/);
});

test('serialization failure publishes no partial native log', async t => {
  const warning = t.mock.method(console, 'warn', () => {});
  const circular = {};
  circular.self = circular;
  const f = await fixture(t, { history: () => [circular] });
  await f.executor.execute(f.stimulus, f.options);
  assert.deepEqual(await readdir(f.logDir), []);
  assert.equal(f.calls.some(([method]) => method === 'delete'), false);
  assert.equal(warning.mock.callCount(), 1);
});

test('failed execution still exports history before disconnect', async t => {
  const failure = new Error('fixture execution failed');
  const f = await fixture(t, { sendError: failure });
  await assert.rejects(f.executor.execute(f.stimulus, f.options), error => error === failure);
  assert.deepEqual(await recordedEvents(f.logDir), f.histories.get('logged-session'));
  assert.deepEqual(f.calls, [
    ['abort', 'logged-session'], ['events', 'logged-session'],
    ['disconnect', 'logged-session'], ['delete', 'logged-session'],
  ]);
});

test('timed-out history retrieval cannot write into a finalized log root', async t => {
  const warning = t.mock.method(console, 'warn', () => {});
  const originalTimeout = CopilotSdkExecutor.CLEANUP_TIMEOUT_MS;
  CopilotSdkExecutor.CLEANUP_TIMEOUT_MS = 25;
  t.after(() => { CopilotSdkExecutor.CLEANUP_TIMEOUT_MS = originalTimeout; });
  let release;
  const f = await fixture(t, {
    history: events => new Promise(resolve => { release = () => resolve(events); }),
  });
  const started = performance.now();
  await f.executor.execute(f.stimulus, f.options);
  assert.ok(performance.now() - started < 1000, 'history retrieval must be bounded');
  assert.deepEqual(await readdir(f.logDir), []);
  release();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(await readdir(f.logDir), []);
  assert.equal(f.calls.some(([method]) => method === 'delete'), false);
  assert.equal(warning.mock.callCount(), 1);
});
