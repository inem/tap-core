// Run trusted legacy Hub with every persistent/application path explicit.
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const config = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const { createHub } = await import(pathToFileURL(config.source + '/probe/hub.js').href);
const hub = createHub({
  host: '127.0.0.1', port: config.hub_port, token: config.token,
  runtimePath: config.source + '/probe/runtime.js',
  adapterRuntimePath: config.source + '/probe/adapter-runtime.js',
  adaptersDir: config.root + '/empty-adapters',
  flowsDir: config.root + '/empty-flows',
  mailboxDir: config.root + '/mailbox', journalPath: config.root + '/bus.jsonl',
});
console.log(JSON.stringify({ ready: true, emptyNeeds: hub.openNeeds.size === 0 }));
