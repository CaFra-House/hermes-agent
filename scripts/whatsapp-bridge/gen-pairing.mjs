// Vincula WhatsApp por CÓDIGO de 8 dígitos (sin QR).
// Uso: node gen-pairing.mjs <session_dir> <numero_internacional_sin_+> <out_file> [segundos]
import { makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion, DisconnectReason } from '@whiskeysockets/baileys';
import { writeFileSync } from 'fs';

const sessionDir = process.argv[2];
const phoneNumber = String(process.argv[3] || '').replace(/\D/g, '');
const outFile = process.argv[4] || '/tmp/wa-pairing.txt';
const seconds = parseInt(process.argv[5] || '300', 10);

const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
const { version } = await fetchLatestBaileysVersion();

const sock = makeWASocket({
  version,
  auth: state,
  printQRInTerminal: false,
  browser: ['Hermes', 'Chrome', '1.0.0'],
});

sock.ev.on('creds.update', saveCreds);

let requested = false;

sock.ev.on('connection.update', async (u) => {
  const { connection, lastDisconnect, qr } = u;

  if (qr && !requested && phoneNumber) {
    requested = true;
    try {
      const code = await sock.requestPairingCode(phoneNumber);
      writeFileSync(outFile, code);
      console.log('PAIRING_CODE=' + code);
    } catch (e) {
      console.log('PAIRING_ERROR=' + e.message);
      process.exit(4);
    }
  }

  if (connection === 'open') {
    console.log('CONNECTED');
    writeFileSync(outFile, 'CONNECTED');
    setTimeout(() => process.exit(0), 1500);
  }

  if (connection === 'close') {
    const code = lastDisconnect?.error?.output?.statusCode;
    console.log('CLOSED code=' + code + ' ' + (lastDisconnect?.error?.message || ''));
    if (code === DisconnectReason.loggedOut) {
      console.log('LOGGED_OUT');
      process.exit(2);
    }
  }
});

setTimeout(() => { console.log('TIMEOUT'); process.exit(3); }, seconds * 1000);
