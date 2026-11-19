import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { lidMappingEntry, resolveInboundSenderPhone } from './bridge_helpers.js';

// The bridge process itself cannot be imported (it opens an HTTP server and a
// Baileys socket at module load), so the LID→phone resolution it runs for every
// inbound DM lives in bridge_helpers.js and is exercised here.  The last test
// asserts the bridge still calls it and stamps the event, so removing the
// wiring cannot silently pass this suite.

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OMAR_LID = '274293875294228';
const OMAR_PHONE = '5491137928875';

// ── lid-mapping file shapes ──────────────────────────────────────────────────

test('reverse mapping file: lid in the name, phone in the contents', () => {
  assert.deepEqual(
    lidMappingEntry(`lid-mapping-${OMAR_LID}_reverse.json`, OMAR_PHONE),
    [OMAR_LID, OMAR_PHONE],
  );
});

test('forward mapping file: phone in the name, lid in the contents', () => {
  assert.deepEqual(
    lidMappingEntry('lid-mapping-5491127349952.json', '10046096330773'),
    ['10046096330773', '5491127349952'],
  );
});

test('non-mapping files and non-numeric contents are ignored', () => {
  assert.equal(lidMappingEntry('creds.json', '5491137928875'), null);
  assert.equal(lidMappingEntry('lid-mapping-123_reverse.json', 'not-a-phone'), null);
  assert.equal(lidMappingEntry('lid-mapping-123.json', ''), null);
});

test('the session map resolves the incident LID from its _reverse file', () => {
  const files = {
    [`lid-mapping-${OMAR_LID}_reverse.json`]: OMAR_PHONE,
    'lid-mapping-5491127349952.json': '10046096330773',
  };
  const lidToPhone = {};
  for (const [filename, contents] of Object.entries(files)) {
    const entry = lidMappingEntry(filename, contents);
    if (entry) lidToPhone[entry[0]] = entry[1];
  }

  assert.equal(
    resolveInboundSenderPhone({
      chatId: `${OMAR_LID}@lid`,
      senderAltId: '',
      lidToPhone,
      isGroup: false,
      fromMe: false,
    }),
    OMAR_PHONE,
  );
});

// ── sender phone resolution ──────────────────────────────────────────────────

test('DM with a phone twin on the envelope resolves from senderAltId', () => {
  assert.equal(
    resolveInboundSenderPhone({
      chatId: '274293875294228@lid',
      senderAltId: '5491137928875@s.whatsapp.net',
      lidToPhone: {},
      isGroup: false,
      fromMe: false,
    }),
    '5491137928875',
  );
});

test('DM addressed only by LID resolves from the session map', () => {
  assert.equal(
    resolveInboundSenderPhone({
      chatId: `${OMAR_LID}@lid`,
      senderAltId: '',
      lidToPhone: { [OMAR_LID]: OMAR_PHONE },
      isGroup: false,
      fromMe: false,
    }),
    OMAR_PHONE,
  );
});

test('an unmapped LID yields an empty phone, never an invented one', () => {
  assert.equal(
    resolveInboundSenderPhone({
      chatId: '999999999999999@lid',
      senderAltId: '',
      lidToPhone: { [OMAR_LID]: OMAR_PHONE },
      isGroup: false,
      fromMe: false,
    }),
    '',
  );
});

test('groups never get a sender phone', () => {
  assert.equal(
    resolveInboundSenderPhone({
      chatId: '120363000000000000@g.us',
      senderAltId: '5491137928875@s.whatsapp.net',
      lidToPhone: { [OMAR_LID]: OMAR_PHONE },
      isGroup: true,
      fromMe: false,
    }),
    '',
  );
});

test('fromMe events never get a sender phone', () => {
  assert.equal(
    resolveInboundSenderPhone({
      chatId: `${OMAR_LID}@lid`,
      senderAltId: '5491137928875@s.whatsapp.net',
      lidToPhone: { [OMAR_LID]: OMAR_PHONE },
      isGroup: false,
      fromMe: true,
    }),
    '',
  );
});

test('phone digits are cleaned of device suffixes and punctuation', () => {
  assert.equal(
    resolveInboundSenderPhone({
      chatId: 'x@s.whatsapp.net',
      senderAltId: '+54 9 11 3792-8875:12@s.whatsapp.net',
      lidToPhone: {},
      isGroup: false,
      fromMe: false,
    }),
    '5491137928875',
  );
});

// ── bridge wiring ────────────────────────────────────────────────────────────

test('bridge.js computes senderPhone per message and stamps the queued event', () => {
  const source = readFileSync(path.join(HERE, 'bridge.js'), 'utf8');
  assert.match(source, /const resolvePhone = \(\) => resolveInboundSenderPhone\(\{/);
  assert.match(source, /let senderPhone = resolvePhone\(\);/);
  assert.match(source, /lidToPhone = buildLidMap\(\);/);
  assert.match(source, /event\.senderPhone = senderPhone;/);
  // The map reader must accept the _reverse shape, or the incident LID stays unresolved.
  assert.match(source, /lidMappingEntry\(f, JSON\.parse/);
});
