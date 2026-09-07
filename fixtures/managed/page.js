// The example owns when to query its projection, not the generic transport.
(() => {
  document.querySelector('#load').onclick = async () => {
    try {
      const response = await fetch(new URL('/record', location.href));
      if (!response.ok) throw new Error('input failed');
      await response.arrayBuffer();
      const expected = response.headers.get('X-Fixture-Value');
      const deadline = Date.now() + 15000;
      while (Date.now() < deadline) {
        try {
          const value = await TapBridge.request('projection', {});
          if (value.value === expected) {
            document.querySelector('#result').textContent = JSON.stringify(value);
            return;
          }
        } catch (error) { if (error.code !== 'not_ready') throw error; }
        await new Promise(resolve => setTimeout(resolve, 200));
      }
      throw new Error('projection not ready');
    } catch (error) { document.querySelector('#result').textContent = 'ERROR: ' + error.message; }
  };
})();
