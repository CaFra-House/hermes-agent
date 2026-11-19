import test from 'node:test';
import assert from 'node:assert/strict';

import {
  parseTakeoverConfig,
  isTakeoverActive,
  isTakeoverActiveForTarget,
  markTakeover,
  pruneExpiredTakeovers,
  DEFAULT_TTL_HOURS,
} from './takeover_gate.js';

const HOUR = 3600_000;

test('parseTakeoverConfig: disabled by default, 12h TTL default', () => {
  const cfg = parseTakeoverConfig({});
  assert.equal(cfg.enabled, false);
  assert.equal(cfg.ttlMs, DEFAULT_TTL_HOURS * HOUR);
  assert.equal(DEFAULT_TTL_HOURS, 12);
});

test('parseTakeoverConfig: env overrides', () => {
  const cfg = parseTakeoverConfig({ WHATSAPP_TAKEOVER: 'true', WHATSAPP_TAKEOVER_TTL_HOURS: '6' });
  assert.equal(cfg.enabled, true);
  assert.equal(cfg.ttlMs, 6 * HOUR);
});

test('markTakeover: sets window; repeat extends (rolling TTL, since preserved)', () => {
  const t0 = 1_000_000;
  let state = markTakeover({}, 'C1@s.whatsapp.net', t0, HOUR, 'Julio');
  assert.equal(state['C1@s.whatsapp.net'].since, t0);
  assert.equal(state['C1@s.whatsapp.net'].until, t0 + HOUR);
  assert.equal(state['C1@s.whatsapp.net'].name, 'Julio');

  // Second owner message inside the window: since preserved, until extended.
  state = markTakeover(state, 'C1@s.whatsapp.net', t0 + 60_000, HOUR, 'Julio');
  assert.equal(state['C1@s.whatsapp.net'].since, t0);
  assert.equal(state['C1@s.whatsapp.net'].until, t0 + 60_000 + HOUR);

  // Message after the window lapsed: new takeover, since resets.
  state = markTakeover(state, 'C1@s.whatsapp.net', t0 + 3 * HOUR, HOUR, 'Julio');
  assert.equal(state['C1@s.whatsapp.net'].since, t0 + 3 * HOUR);
});

test('isTakeoverActive: true inside window, false after expiry, per-chat', () => {
  const t0 = 1_000_000;
  const state = markTakeover({}, 'C1', t0, HOUR, '');
  assert.equal(isTakeoverActive(state, 'C1', t0 + 10), true);
  assert.equal(isTakeoverActive(state, 'C1', t0 + HOUR + 1), false);
  assert.equal(isTakeoverActive(state, 'C2', t0 + 10), false);
  assert.equal(isTakeoverActive({}, 'C1', t0), false);
});

test('isTakeoverActiveForTarget: phone↔LID alias matching for outbound sends', () => {
  const t0 = 1_000_000;
  // Takeover keyed by the inbound LID JID; sender targets the phone JID.
  const state = markTakeover({}, '998877665544@lid', t0, HOUR, 'Julio');
  const aliases = (jid) => new Set([jid.replace(/@.*/, ''), '998877665544']); // phone→LID expansion
  assert.equal(isTakeoverActiveForTarget(state, '5491122334455@s.whatsapp.net', t0 + 10, aliases), true);
  assert.equal(isTakeoverActiveForTarget(state, '5491199998888@s.whatsapp.net', t0 + 10, () => new Set(['5491199998888'])), false);
  // Expired entry never blocks.
  assert.equal(isTakeoverActiveForTarget(state, '5491122334455@s.whatsapp.net', t0 + HOUR + 1, aliases), false);
  // Empty state never blocks.
  assert.equal(isTakeoverActiveForTarget({}, '5491122334455@s.whatsapp.net', t0, aliases), false);
  // Fallback (no aliasesOf): plain numeric match works too.
  const state2 = markTakeover({}, '5491122334455@s.whatsapp.net', t0, HOUR, 'J');
  assert.equal(isTakeoverActiveForTarget(state2, '5491122334455@s.whatsapp.net', t0 + 10), true);
});

test('pruneExpiredTakeovers: keeps live entries, drops expired', () => {
  const t0 = 1_000_000;
  const state = {
    LIVE: { since: t0, until: t0 + HOUR, name: 'a' },
    DEAD: { since: t0, until: t0 - 1, name: 'b' },
  };
  const pruned = pruneExpiredTakeovers(state, t0);
  assert.deepEqual(Object.keys(pruned), ['LIVE']);
});
