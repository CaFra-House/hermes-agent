// Genera el QR de re-vinculación de WhatsApp para un perfil Hermes.
// Uso: node gen-qr.mjs <session_dir> <out_file> [segundos]
import { makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion, DisconnectReason } from '@whiskeysockets/baileys';
import { writeFileSync } from 'fs';
import path from 'path';

const sessionDir = process.argv[2];
const outFile = process.argv[3] || '/tmp/wa-qr.txt';
const seconds = parseInt(process.argv[4] || '45', 10);

const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
const { version } = await fetchLatestBaileysVersion();

const sock = makeWASocket({
  version,
  auth: state,
  printQRInTerminal: false,
  browser: ['Hermes', 'Chrome', '1.0.0'],
});

let done = false;
sock.ev.on('creds.update', saveCreds);

sock.ev.on('connection.update', (u) => {
  const { connection, lastDisconnect, qr } = u;
  if (qr) {
    writeFileSync(outFile, qr);
    console.log('QR_READY');
  }
  if (connection === 'open') {
    console.log('CONNECTED');
    done = true;
    setTimeout(() => process.exit(0), 1500);
  }
  if (connection === 'close') {
    const code = lastDisconnect?.error?.output?.statusCode;
    console.log('CLOSED code=' + code + ' reason=' + (lastDisconnect?.error?.message || ''));
    if (code === DisconnectReason.loggedOut) {
      console.log('LOGGED_OUT');
      process.exit(2);
    }
  }
});

setTimeout(() => {
  if (!done) { console.log('TIMEOUT'); process.exit(3); }
}, seconds * 1000);
