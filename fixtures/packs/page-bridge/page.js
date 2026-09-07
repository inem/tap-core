// The host supplies the bridge and calls these exports. No WS URL or token here.
export async function start({ bridge, document }) {
  const reply = await bridge.request({ op: "echo", text: "hello" });
  if (reply.ok !== true || typeof reply.text !== "string") throw new Error("bad local reply");
  document.documentElement.dataset.tapFixture = reply.text;
}

export function stop({ document }) {
  delete document.documentElement.dataset.tapFixture;
}
