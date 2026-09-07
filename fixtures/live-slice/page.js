// Injected fixture code. Origin HTML contains controls but no TAP implementation.
(() => {
  TapProbe.registerAction('fixture.render', ({ record_id, value }) => {
    document.querySelector('#result').textContent = JSON.stringify({ record_id, value });
    return { rendered: true, record_id, value };
  });
  TapProbe.registerAction('fixture.confirm', () => {
    document.querySelector('#confirmed').textContent = 'confirmed';
    return { confirmed: true };
  });
  document.querySelector('#load').onclick = async () => {
    // Consume and discard the response: the displayed value must return from
    // the saved reader projection through the Hub command, not this fetch.
    const response = await fetch(new URL('/record', location.href));
    if (!response.ok) throw new Error('fixture response failed');
    await response.arrayBuffer();
    await TapProbe.emitEvent('fixture.output.requested', { path: '/record' });
  };
})();
