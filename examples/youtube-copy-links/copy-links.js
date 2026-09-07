(function(){
  'use strict';
  if (window.__tapYoutubeCopyLinks) return;
  if (!window.YouTubeUI) return;

  var UI = window.YouTubeUI;
  var state = window.__tapYoutubeCopyLinks = { lastCaptionRequest: null };
  var COPY_PATH = 'M16 1H4a2 2 0 0 0-2 2v14h2V3h12V1zm3 4H8a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2zm0 16H8V7h11v14z';
  var CHECK_PATH = 'M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z';
  var ERROR_PATH = 'm6.4 5 5.6 5.6L17.6 5 19 6.4 13.4 12l5.6 5.6-1.4 1.4-5.6-5.6L6.4 19 5 17.6l5.6-5.6L5 6.4 6.4 5z';
  var LOAD_PATH = 'M11 4h2v9l3.5-3.5 1.4 1.4L12 16.8l-5.9-5.9 1.4-1.4L11 13V4zm-5 15h12v2H6v-2z';
  var COPY_ALL_PATH = 'M7 2h10v2H7V2zM4 6h16v2H4V6zm2 4h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2zm0 2v8h12v-8H6z';

  function icon(pathData) {
    var namespace = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(namespace, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', '24');
    svg.setAttribute('height', '24');
    svg.setAttribute('aria-hidden', 'true');
    svg.style.cssText = 'display:block;pointer-events:none';
    var path = document.createElementNS(namespace, 'path');
    path.setAttribute('fill', 'currentColor');
    path.setAttribute('d', pathData);
    svg.appendChild(path);
    return svg;
  }

  function legacyCopy(text) {
    var textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.cssText = 'position:fixed;opacity:0;left:-9999px;top:0';
    document.body.appendChild(textarea);
    textarea.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (_) {}
    textarea.remove();
    return ok;
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).catch(function(){
        if (!legacyCopy(text)) throw new Error('copy failed');
      });
    }
    return legacyCopy(text) ? Promise.resolve() : Promise.reject(new Error('copy failed'));
  }

  function flash(button, stateName, message) {
    if (!button) return;
    clearTimeout(button.__tapFlashTimer);
    if (!button.__tapCopyLabel) button.__tapCopyLabel = button.getAttribute('aria-label') || 'Copy link';
    button.dataset.state = stateName;
    UI.setButtonIcon(button, icon(stateName === 'success' ? CHECK_PATH : ERROR_PATH));
    button.setAttribute('aria-label', message);
    button.title = message;
    button.__tapFlashTimer = setTimeout(function(){
      delete button.dataset.state;
      UI.setButtonIcon(button, icon(COPY_PATH));
      button.setAttribute('aria-label', button.__tapCopyLabel);
      button.title = button.__tapCopyLabel;
    }, 1100);
  }

  function playerResponse(videoId) {
    var initial = window.ytInitialPlayerResponse;
    if (initial && initial.videoDetails && initial.videoDetails.videoId === videoId) {
      return Promise.resolve(initial);
    }
    var config = window.ytcfg;
    var get = config && typeof config.get === 'function'
      ? function(key){ return config.get(key); }
      : function(){ return null; };
    var key = get('INNERTUBE_API_KEY');
    var context = get('INNERTUBE_CONTEXT');
    if (!key || !context) return Promise.reject(new Error('player config unavailable'));
    var headers = { 'content-type': 'application/json' };
    var clientName = get('INNERTUBE_CONTEXT_CLIENT_NAME');
    var clientVersion = get('INNERTUBE_CONTEXT_CLIENT_VERSION');
    if (clientName) headers['x-youtube-client-name'] = String(clientName);
    if (clientVersion) headers['x-youtube-client-version'] = String(clientVersion);
    return fetch('/youtubei/v1/player?key=' + encodeURIComponent(key) + '&prettyPrint=false', {
      method: 'POST',
      credentials: 'same-origin',
      headers: headers,
      body: JSON.stringify({
        context: context,
        videoId: videoId,
        contentCheckOk: true,
        racyCheckOk: true
      })
    }).then(function(response){
      if (!response.ok) throw new Error('player request failed');
      return response.json();
    });
  }

  function preferredCaption(tracks) {
    if (!tracks || !tracks.length) return null;
    var language = ((document.documentElement && document.documentElement.lang) || '').split('-')[0];
    return tracks.find(function(track){
      return track.languageCode === language && track.kind !== 'asr';
    }) || tracks.find(function(track){
      return track.languageCode === language;
    }) || tracks.find(function(track){
      return track.languageCode === 'en' && track.kind !== 'asr';
    }) || tracks.find(function(track){
      return track.kind !== 'asr';
    }) || tracks[0];
  }

  function requestCaptions(videoId) {
    var requestState = state.lastCaptionRequest = {
      videoId: videoId,
      status: 'requesting'
    };
    return playerResponse(videoId).then(function(player){
      var renderer = player && player.captions && player.captions.playerCaptionsTracklistRenderer;
      var track = preferredCaption(renderer && renderer.captionTracks);
      if (!track || !track.baseUrl) {
        requestState.status = 'unavailable';
        return false;
      }
      requestState.trackUrl = track.baseUrl;
      return fetch(track.baseUrl, { credentials: 'include' }).then(function(response){
        return response.text().then(function(){
          requestState.status = response.ok ? 'captured' : 'failed';
          return response.ok;
        });
      });
    }).catch(function(error){
      requestState.status = 'failed';
      requestState.error = String(error && error.message || error);
      return false;
    });
  }

  function copyEntry(entry, button) {
    var copied = copyText(entry.shortUrl);
    requestCaptions(entry.id);
    copied.then(
      function(){
        UI.flashVideoTarget(entry);
        UI.showToast('Link copied');
        flash(button, 'success', 'Link copied');
      },
      function(){
        UI.showToast('Copy failed · try again');
        flash(button, 'error', 'Copy failed · try again');
      }
    );
  }

  function mountCard(entry) {
    var button = UI.addVideoCardAction(entry, {
      id: 'tap-copy-link',
      icon: icon(COPY_PATH),
      title: 'Copy video URL ' + entry.shortUrl,
      ariaLabel: 'Copy video URL ' + entry.shortUrl,
      onClick: function(current, event, button) {
        copyEntry(current, button);
      }
    });
    if (button) button.__tapCopyLabel = 'Copy video URL ' + entry.shortUrl;
    return button;
  }

  function isPlaylistPage() {
    return location.pathname.indexOf('/playlist') === 0;
  }

  function entries() {
    return UI.getVideoEntries(document, { unique: true });
  }

  function actionButton(id) {
    var element = UI.getElement(id);
    if (!element) return null;
    return element.matches && element.matches('button') ? element : element.querySelector('button');
  }

  function clearLegacyMastheadButtons() {
    ['tap-youtube-load-all', 'tap-youtube-copy-all'].forEach(function(id){
      var element = UI.getElement(id);
      if (element && !element.dataset.youtubeUiPlaylistAction) UI.removeElement(id);
    });
  }

  function syncCollectionActions() {
    clearLegacyMastheadButtons();
    if (!isPlaylistPage()) {
      UI.removeElement('tap-youtube-load-all');
      UI.removeElement('tap-youtube-copy-all');
      return;
    }

    UI.addPlaylistAction({
      id: 'tap-youtube-copy-all',
      icon: icon(COPY_ALL_PATH),
      title: 'Copy all loaded video links (' + entries().length + ')',
      ariaLabel: 'Copy all loaded video links (' + entries().length + ')',
      onClick: function(event, button){ copyAll(button); }
    });
    UI.addPlaylistAction({
      id: 'tap-youtube-load-all',
      icon: icon(LOAD_PATH),
      title: 'Load every video in this playlist',
      ariaLabel: 'Load every video in this playlist',
      onClick: function(event, button){ loadAll(button); }
    });

    var copyButton = actionButton('tap-youtube-copy-all');
    if (copyButton) {
      var count = entries().length;
      copyButton.title = 'Copy all loaded video links (' + count + ')';
      copyButton.setAttribute('aria-label', 'Copy all loaded video links (' + count + ')');
    }
    var loadButton = actionButton('tap-youtube-load-all');
    if (loadButton && !state.loading) {
      loadButton.title = 'Load every video in this playlist';
      loadButton.setAttribute('aria-label', 'Load every video in this playlist');
    }
  }

  async function loadAll(button) {
    if (state.loading) return;
    state.loading = true;
    button = button || actionButton('tap-youtube-load-all');
    if (button) {
      button.disabled = true;
      button.setAttribute('aria-disabled', 'true');
      button.setAttribute('aria-busy', 'true');
      button.title = 'Loading every video in this playlist…';
      button.setAttribute('aria-label', button.title);
    }
    var previousHeight = 0;
    var previousCount = 0;
    var stable = 0;

    for (var iteration = 0; iteration < 500 && stable < 4 && isPlaylistPage(); iteration++) {
      window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'auto' });
      await new Promise(function(resolve){ setTimeout(resolve, 1000); });
      var count = entries().length;
      var height = document.documentElement.scrollHeight;
      UI.showToast('Loaded ' + count + ' videos…', {
        id: 'tap-youtube-progress', duration: 0
      });
      if (height === previousHeight && count === previousCount) stable++;
      else stable = 0;
      previousHeight = height;
      previousCount = count;
    }

    state.loading = false;
    var loadedCount = entries().length;
    UI.showToast('Loaded ' + loadedCount + ' videos', {
      id: 'tap-youtube-progress', duration: 1800
    });
    if (button && button.isConnected) {
      button.disabled = false;
      button.removeAttribute('aria-busy');
      button.setAttribute('aria-disabled', 'false');
      UI.setButtonIcon(button, icon(CHECK_PATH));
      button.title = 'Loaded ' + loadedCount + ' videos';
      button.setAttribute('aria-label', button.title);
      setTimeout(function(){
        if (!button.isConnected) return;
        UI.setButtonIcon(button, icon(LOAD_PATH));
        syncCollectionActions();
      }, 1200);
    }
  }

  function copyAll(button) {
    var all = entries();
    var text = all.map(function(entry){ return entry.shortUrl; }).join('\n');
    if (!text) {
      UI.showToast('No video links found');
      if (button) {
        UI.setButtonIcon(button, icon(ERROR_PATH));
        setTimeout(function(){ if (button.isConnected) UI.setButtonIcon(button, icon(COPY_ALL_PATH)); }, 1200);
      }
      return;
    }
    copyText(text).then(
      function(){
        UI.showToast('Copied ' + all.length + ' video links');
        if (button) UI.setButtonIcon(button, icon(CHECK_PATH));
        restoreCopyAll(button);
      },
      function(){
        UI.showToast('Copy failed');
        if (button) UI.setButtonIcon(button, icon(ERROR_PATH));
        restoreCopyAll(button);
      }
    );
  }

  function restoreCopyAll(button) {
    setTimeout(function(){
      if (!button || !button.isConnected) return;
      UI.setButtonIcon(button, icon(COPY_ALL_PATH));
      syncCollectionActions();
    }, 1200);
  }

  function mountCurrentPage() {
    var id = UI.getVideoId(location.href);
    if (!id) return null;
    var entry = {
      id: id,
      url: location.href,
      shortUrl: 'https://youtu.be/' + id,
      element: document.documentElement,
      host: document.documentElement
    };
    var options = {
      id: 'tap-copy-current-video',
      icon: icon(COPY_PATH),
      label: 'Copy',
      title: 'Copy video URL ' + entry.shortUrl,
      ariaLabel: 'Copy video URL ' + entry.shortUrl,
      onClick: function(current, event, button){ copyEntry(current, button); }
    };
    var button = location.pathname.indexOf('/shorts/') === 0
      ? UI.addShortsAction(entry, options)
      : UI.addWatchVideoAction(entry, options);
    if (button) button.__tapCopyLabel = options.ariaLabel;
    return button;
  }

  function syncCurrentPage() {
    clearInterval(state.currentTimer);
    UI.removeElement('tap-copy-current-video');
    document.querySelectorAll('[data-youtube-ui-shorts-action="tap-copy-current-video"]')
      .forEach(function(element){ element.remove(); });
    var attempts = 0;
    mountCurrentPage();
    state.currentTimer = setInterval(function(){
      attempts++;
      mountCurrentPage();
      if (attempts >= 32) clearInterval(state.currentTimer);
    }, 50);
  }

  function isTextCopyContext(event) {
    var target = event.target;
    if (target && target.closest && target.closest(
      'input, textarea, [contenteditable="true"], [role="textbox"]'
    )) return true;
    var selection = window.getSelection && window.getSelection();
    return Boolean(selection && !selection.isCollapsed && String(selection).length);
  }

  function onCopyShortcut(event) {
    if (event.repeat || (!event.ctrlKey && !event.metaKey) || event.altKey || event.shiftKey) return;
    var key = String(event.key).toLowerCase();
    if (event.code !== 'KeyC' && key !== 'c' && key !== 'с') return;
    if (!state.hoveredEntry || isTextCopyContext(event)) return;
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    copyEntry(state.hoveredEntry, null);
  }

  state.stopCards = UI.onVideoCards(mountCard, { repeat: true, timeoutMs: 1600 });
  state.stopHover = UI.onVideoHover(function(entry){ state.hoveredEntry = entry; }, { highlight: true });
  window.addEventListener('keydown', onCopyShortcut, true);
  window.addEventListener('yt-navigate-finish', syncCurrentPage);
  window.addEventListener('yt-page-data-updated', syncCurrentPage);
  syncCurrentPage();
})();
