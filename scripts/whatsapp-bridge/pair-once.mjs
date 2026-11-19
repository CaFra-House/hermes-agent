// Código de vinculación de un solo uso: abre el socket, pide el código y
// ESPERA sin cerrar (no cicla, así el código no se invalida por reconexión).
// Uso: node pair-once.mjs <session_dir> <numero> <out_file> [minutos]
import { makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion, DisconnectReason } from '@whiskeysockets/baileys';
import { writeFileSync } from 'fs';

const sessionDir = process.argv[2];
const phoneNumber = String(process.argv[3] || '').replace(/\D/g, '');
const outFile = process.argv[4] || '/tmp/wa-code.txt';
const minutes = parseFloat(process.argv[5] || '12');

const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
const { version } = await fetchLatestBaileysVersion();

const sock = makeWASocket({
  version,
  auth: state,
  printQRInTerminal: false,
  browser: ['Hermes', 'Chrome', '1.0.0'],
});

sock.ev.on('creds.update', saveCreds);

let asked = false;

const askTimer = setTimeout(async () => {
  if (asked) return;
  asked = true;
  try {
    const code = await sock.requestPairingCode(phoneNumber);
    writeFileSync(outFile, `${code}\n${Date.now()}`);
    console.log('CODE=' + code);
  } catch (e) {
    console.log('PAIRING_ERROR=' + e.message);
    writeFileSync(outFile, `ERROR ${e.message}`);
  }
}, 3000);

sock.ev.on('connection.update', (u) => {
  const { connection, lastDisconnect } = u;
  if (connection === 'open') {
    clearTimeout(askTimer);
    writeFileSync(outFile, `CONNECTED\n${Date.now()}`);
    console.log('CONNECTED');
    setTimeout(() => process.exit(0), 2000);
  }
  if (connection === 'close') {
    const code = lastDisconnect?.error?.output?.statusCode;
    console.log('CLOSED code=' + code);
    if (code === DisconnectReason.loggedOut) {
      console.log('LOGGED_OUT');
      process.exit(2);
    }
  }
});

setTimeout(() => { console.log('TIMEOUT'); process.exit(3); }, minutes * 60_000);
