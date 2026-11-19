import test from 'node:test';
import assert from 'node:assert/strict';

import {
  createInboundTextTracker,
  inboundDedupeKey,
  inboundTextDedupeKey,
  normalizeInboundText,
} from './bridge_helpers.js';
import { createOutboundIdTracker } from './outbound_ids.js';

// bridge.js cannot be imported (it opens an HTTP server and a Baileys socket at
// module load), so the predicates and trackers it uses to drop repeat inbound
// messages live in bridge_helpers.js and are exercised here.

const CHAT = '5491137928875@s.whatsapp.net';
const OTHER_CHAT = '573185702102@s.whatsapp.net';
const TEXT = '¡Hola! Quiero más información';

/**
 * The exact guard bridge.js runs before messageQueue.push: id first (elsewhere),
 * then content.  Returns true when the event reaches the queue.
 */
function enqueue(queued, tracker, event) {
  const key = inboundTextDedupeKey(event);
  if (key) {
    if (tracker.has(key)) return false;
    tracker.remember(key);
  }
  queued.push(event);
  return true;
}

// ── id guard (unchanged) ─────────────────────────────────────────────────────

test('keys a normal inbound message by chat + message id', () => {
  assert.equal(
    inboundDedupeKey({ chatId: '573185702102@s.whatsapp.net', messageId: 'AC55F6A6', fromMe: false }),
    '573185702102@s.whatsapp.net:AC55F6A6',
  );
});

test('never keys fromMe events (echo / owner handling stays untouched)', () => {
  assert.equal(inboundDedupeKey({ chatId: '573185702102@s.whatsapp.net', messageId: 'AC55F6A6', fromMe: true }), null);
});

test('never keys an event without a message id', () => {
  assert.equal(inboundDedupeKey({ chatId: '573185702102@s.whatsapp.net', messageId: '', fromMe: false }), null);
  assert.equal(inboundDedupeKey({ chatId: '573185702102@s.whatsapp.net', fromMe: false }), null);
  assert.equal(inboundDedupeKey(), null);
});

test('same text from the customer with a different id yields a different key', () => {
  const first = inboundDedupeKey({ chatId: CHAT, messageId: 'AAA', fromMe: false });
  const second = inboundDedupeKey({ chatId: CHAT, messageId: 'BBB', fromMe: false });
  assert.notEqual(first, second);
});

test('same message id in another chat yields a different key', () => {
  const dm = inboundDedupeKey({ chatId: CHAT, messageId: 'AAA', fromMe: false });
  const other = inboundDedupeKey({ chatId: OTHER_CHAT, messageId: 'AAA', fromMe: false });
  assert.notEqual(dm, other);
});

test('the bridge tracker drops the second copy of one message', () => {
  const recentlyProcessedInbound = createOutboundIdTracker(1024);
  const key = inboundDedupeKey({ chatId: CHAT, messageId: 'AC55F6A6', fromMe: false });

  assert.equal(recentlyProcessedInbound.has(key), false);
  recentlyProcessedInbound.remember(key);
  assert.equal(recentlyProcessedInbound.has(key), true); // 'append' copy queued, 'notify' copy dropped
});

// ── content guard ────────────────────────────────────────────────────────────

test('normalizes by trimming and collapsing whitespace only', () => {
  assert.equal(normalizeInboundText('  ¡Hola!   Quiero\nmás   información \n'), TEXT);
  assert.equal(normalizeInboundText('Hola'), 'Hola');
  assert.equal(normalizeInboundText('hola'), 'hola'); // case is preserved: a different message
  assert.equal(normalizeInboundText(undefined), '');
  assert.equal(normalizeInboundText(null), '');
});

test('the incident pair (conversation vs extendedTextMessage, 2 s apart) queues one copy', () => {
  let clock = 0;
  const tracker = createInboundTextTracker({ windowMs: 6000, now: () => clock });
  const first = { chatId: CHAT, messageId: 'AC9E100A40274AEB0BFFE15207A09993', nativeType: 'extendedTextMessage', body: TEXT };
  const second = { chatId: CHAT, messageId: 'AC48A2781E29B7A36870A57EAC6E2B4E', nativeType: 'conversation', body: TEXT };
  const queued = [];

  // Different ids: the id guard alone would let both through.
  assert.notEqual(inboundDedupeKey(first), inboundDedupeKey(second));

  assert.equal(enqueue(queued, tracker, first), true);
  clock = 2000; // WhatsApp's automatic retransmission, measured at 2.0 s
  assert.equal(enqueue(queued, tracker, second), false);
  assert.deepEqual(queued.map((event) => event.messageId), [first.messageId]);
});

test('the same text outside the 6 s window passes both times', () => {
  let clock = 0;
  const tracker = createInboundTextTracker({ windowMs: 6000, now: () => clock });
  const queued = [];
  const first = { chatId: CHAT, messageId: 'AAA', body: TEXT };
  const second = { chatId: CHAT, messageId: 'BBB', body: TEXT };

  assert.equal(enqueue(queued, tracker, first), true);
  clock = 8001; // 8.0 s later: a genuinely repeated message, not a retransmission
  assert.equal(enqueue(queued, tracker, second), true);
  assert.deepEqual(queued.map((event) => event.messageId), ['AAA', 'BBB']);
});

test('the same text in another chat passes both times', () => {
  let clock = 0;
  const tracker = createInboundTextTracker({ windowMs: 6000, now: () => clock });
  const queued = [];

  assert.equal(enqueue(queued, tracker, { chatId: CHAT, messageId: 'AAA', body: TEXT }), true);
  clock = 2000;
  assert.equal(enqueue(queued, tracker, { chatId: OTHER_CHAT, messageId: 'BBB', body: TEXT }), true);
  assert.equal(queued.length, 2);
});

test('whitespace differences still count as the same text', () => {
  let clock = 0;
  const tracker = createInboundTextTracker({ windowMs: 6000, now: () => clock });
  const queued = [];

  assert.equal(enqueue(queued, tracker, { chatId: CHAT, messageId: 'AAA', body: TEXT }), true);
  clock = 1000;
  assert.equal(enqueue(queued, tracker, { chatId: CHAT, messageId: 'BBB', body: `  ¡Hola!  Quiero\nmás información  ` }), false);
  assert.equal(queued.length, 1);
});

test('media is never deduplicated by text', () => {
  const tracker = createInboundTextTracker({ windowMs: 6000, now: () => 0 });
  const queued = [];
  const mediaEvents = [
    { chatId: CHAT, messageId: 'M1', body: 'mirá', hasMedia: true },
    { chatId: CHAT, messageId: 'M2', body: 'mirá', hasMedia: true },
    { chatId: CHAT, messageId: 'M3', body: '', mediaType: 'image' },
    { chatId: CHAT, messageId: 'M4', body: '', mediaUrls: ['/cache/image/1.jpg'] },
    { chatId: CHAT, messageId: 'M5', body: '', quotedMediaUrls: ['/cache/image/2.jpg'] },
  ];

  for (const event of mediaEvents) {
    assert.equal(inboundTextDedupeKey(event), null);
    assert.equal(enqueue(queued, tracker, event), true);
  }
  assert.equal(queued.length, mediaEvents.length);
});

test('empty bodies are never deduplicated by text', () => {
  assert.equal(inboundTextDedupeKey({ chatId: CHAT, body: '' }), null);
  assert.equal(inboundTextDedupeKey({ chatId: CHAT, body: '   \n\t ' }), null);
  assert.equal(inboundTextDedupeKey({ chatId: CHAT }), null);
  assert.equal(inboundTextDedupeKey(), null);
});

test('re-remembering the same text does not extend its suppression window', () => {
  let clock = 0;
  const tracker = createInboundTextTracker({ windowMs: 6000, now: () => clock });
  const key = inboundTextDedupeKey({ chatId: CHAT, body: TEXT });

  tracker.remember(key);
  clock = 5000;
  tracker.remember(key); // a burst must not refresh its own timestamp
  clock = 6000;
  assert.equal(tracker.has(key), false);
});

test('the tracker stays bounded', () => {
  const tracker = createInboundTextTracker({ windowMs: 6000, maxSize: 2, now: () => 0 });

  tracker.remember('a');
  tracker.remember('b');
  tracker.remember('c');
  assert.equal(tracker.size(), 2);
  assert.equal(tracker.has('a'), false); // evicted, not suppressed
});
