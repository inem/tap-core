/**
 * YouTube UI Library — small MAIN-world DOM adapter for YouTube watch/browse UI.
 *
 * The public surface mirrors chatgpt-ui.js: selectors live in one place,
 * callers use semantic methods, and the library is exposed as window.YouTubeUI.
 * Selectors deliberately prefer YouTube custom elements, stable ids, ARIA
 * labels, data attributes and structural anchors over generated class names.
 *
 * Quick live check:
 *   YouTubeUI.smokeTest();
 */

(function() {
  'use strict';

  const YouTubeUI = {
    selectors: {
      app: 'ytd-app',
      watchPage: 'ytd-watch-flexy[role="main"], ytd-watch-flexy',

      playerHost: 'ytd-player#ytd-player',
      player: '#movie_player[aria-label], #movie_player',
      video: '#movie_player video',
      playerLeftControls: '#movie_player .ytp-left-controls',
      playerRightControls: '#movie_player .ytp-right-controls',

      metadata: 'ytd-watch-metadata',
      title: 'ytd-watch-metadata h1 yt-formatted-string, ytd-watch-metadata h1',
      titleArea: 'ytd-watch-metadata #title, ytd-watch-metadata h1',
      owner: 'ytd-watch-metadata ytd-video-owner-renderer',
      channelLink: 'ytd-video-owner-renderer #channel-name a[href], ytd-video-owner-renderer a[href^="/@"]',
      channelAvatar: 'ytd-video-owner-renderer #avatar a[href], ytd-video-owner-renderer a[href^="/@"]',
      subscriberCount: 'ytd-video-owner-renderer #owner-sub-count',
      subscribeButton: 'ytd-watch-metadata #subscribe-button ytd-subscribe-button-renderer button[aria-label], ytd-watch-metadata #subscribe-button button[aria-label], ytd-watch-metadata ytd-subscribe-button-renderer button',

      description: 'ytd-watch-metadata ytd-text-inline-expander#description-inline-expander',
      descriptionText: 'ytd-text-inline-expander#description-inline-expander #attributed-snippet-text, ytd-text-inline-expander#description-inline-expander #snippet-text',
      descriptionExpand: 'ytd-text-inline-expander#description-inline-expander #expand[role="button"]',
      descriptionCollapse: 'ytd-text-inline-expander#description-inline-expander #collapse[role="button"]',

      engagementControls: 'ytd-watch-metadata #actions ytd-menu-renderer, ytd-watch-metadata #actions',
      engagementInsert: 'ytd-watch-metadata #actions #top-level-buttons-computed, ytd-watch-metadata #actions ytd-menu-renderer',
      likeButton: 'ytd-watch-metadata like-button-view-model button[aria-label]',
      dislikeButton: 'ytd-watch-metadata dislike-button-view-model button[aria-label]',
      shareButton: 'ytd-watch-metadata #actions button[aria-label="Share"]',
      saveButton: 'ytd-watch-metadata #actions button[aria-label="Save to playlist"]',
      moreActionsButton: 'ytd-watch-metadata #actions button[aria-label="More actions"]',

      comments: 'ytd-comments#comments',
      commentsHeader: 'ytd-comments ytd-comments-header-renderer',
      commentThread: 'ytd-comments ytd-comment-thread-renderer',
      comment: 'ytd-comments ytd-comment-view-model, ytd-comments ytd-comment-renderer',
      commentText: 'ytd-comment-view-model #content-text, ytd-comment-renderer #content-text',
      commentActions: 'ytd-comment-view-model #action-buttons, ytd-comment-renderer #action-buttons, ytd-comment-action-buttons-renderer',
      commentComposer: 'ytd-comments ytd-comment-simplebox-renderer',
      commentInput: 'ytd-comment-simplebox-renderer #contenteditable-root[contenteditable="true"], ytd-comment-simplebox-renderer #simplebox-placeholder',

      transcriptSection: 'ytd-video-description-transcript-section-renderer',
      transcriptButton: 'ytd-video-description-transcript-section-renderer button[aria-label="Show transcript"], ytd-video-description-transcript-section-renderer button',
      transcriptPanel: 'ytd-engagement-panel-section-list-renderer[target-id="engagement-panel-searchable-transcript"]',
      transcriptSegment: 'ytd-engagement-panel-section-list-renderer[target-id="engagement-panel-searchable-transcript"] ytd-transcript-segment-renderer',

      chaptersSection: 'ytd-video-description-chapters-section-renderer',
      chaptersPanel: 'ytd-engagement-panel-section-list-renderer[target-id="engagement-panel-macro-markers-description-chapters"]',
      chapterItem: 'ytd-macro-markers-list-item-renderer',

      masthead: 'ytd-masthead#masthead[role="banner"], ytd-masthead#masthead',
      mastheadButtons: 'ytd-masthead#masthead #end #buttons, ytd-masthead#masthead #end',
      homeLink: 'ytd-masthead ytd-topbar-logo-renderer a#logo[href="/"]',
      guideButton: 'ytd-masthead yt-icon-button#guide-button button[aria-label], ytd-masthead #guide-button button[aria-label]',
      navigation: 'ytd-guide-renderer[role="navigation"], ytd-mini-guide-renderer[role="navigation"]',
      searchBox: 'ytd-masthead yt-searchbox[role="search"], ytd-masthead ytd-searchbox',
      searchInput: 'ytd-masthead yt-searchbox input[role="combobox"], ytd-masthead input#search',
      searchButton: 'ytd-masthead yt-searchbox button[aria-label="Search"], ytd-masthead button#search-icon-legacy',

      playlistPage: 'ytd-browse[page-subtype="playlist"]',
      playlistHeader: 'ytd-browse[page-subtype="playlist"] yt-page-header-renderer',
      playlistFlexibleActions: 'ytd-browse[page-subtype="playlist"] yt-page-header-renderer yt-flexible-actions-view-model',

      videoCard: 'yt-lockup-view-model, ytm-shorts-lockup-view-model, ytm-shorts-lockup-view-model-v2, ytd-rich-item-renderer, ytd-video-renderer, ytd-grid-video-renderer, ytd-compact-video-renderer, ytd-playlist-video-renderer, ytd-playlist-panel-video-renderer',
      videoThumbnailLink: 'a#thumbnail[href*="/watch?v="], yt-lockup-view-model a[href*="/watch?v="], a[href^="/shorts/"]',
      videoTitleLink: 'a#video-title[href*="/watch?v="], a#video-title-link[href*="/watch?v="], a[href*="/watch?v="][title], a[href^="/shorts/"][title]',
      videoThumbnail: 'yt-thumbnail-view-model, ytd-thumbnail, a#thumbnail[href*="/watch?v="]',
      videoCardMetadata: 'yt-lockup-metadata-view-model',
      videoCardMenuButton: 'button[aria-label="More actions"], button[aria-label="Action menu"], yt-lockup-metadata-view-model button[aria-label], ytd-menu-renderer button[aria-label], #menu button[aria-label]',
      videoCardActionRail: 'yt-thumbnail-hover-overlay-toggle-actions-view-model, #hover-overlays',
      videoPreview: 'ytd-app #video-preview ytd-video-preview, ytd-video-preview',
      videoPreviewControls: 'yt-inline-player-controls',
      videoPreviewActionRail: 'yt-inline-player-controls .ytInlinePlayerControlsTopRightControls',
      shortsRenderer: 'ytd-reel-video-renderer',
      shortsActionRail: 'reel-action-bar-view-model',
      shortsShareButton: 'reel-action-bar-view-model button[aria-label="Share"]',
    },

    debugAnchorNames: [
      'watchPage', 'player', 'video', 'playerRightControls', 'title', 'owner', 'channelLink',
      'subscribeButton', 'description', 'engagementControls', 'likeButton',
      'dislikeButton', 'comments', 'commentComposer', 'transcriptPanel',
      'chaptersPanel', 'masthead', 'mastheadButtons', 'searchInput', 'searchButton', 'navigation',
      'videoCard', 'videoThumbnailLink',
    ],

    selector(nameOrSelector) {
      return this.selectors[nameOrSelector] || nameOrSelector;
    },

    find(nameOrSelector, root = document) {
      return root?.querySelector?.(this.selector(nameOrSelector)) || null;
    },

    findAll(nameOrSelector, root = document) {
      return Array.from(root?.querySelectorAll?.(this.selector(nameOrSelector)) || []);
    },

    isVisible(element) {
      if (!element?.isConnected) return false;
      const style = window.getComputedStyle(element);
      return element.getClientRects().length > 0
        && style.display !== 'none'
        && style.visibility !== 'hidden';
    },

    findVisible(nameOrSelector, root = document) {
      return this.findAll(nameOrSelector, root).find((element) => this.isVisible(element)) || null;
    },

    absoluteUrl(href) {
      if (!href) return null;
      try {
        return new URL(href, window.location.origin).href;
      } catch (_) {
        return null;
      }
    },

    // ============================================
    // Extension element management
    // ============================================

    _toasts: {},

    getElement(id) {
      if (!id) return null;
      return this.findAll('[data-youtube-ui-id]').find((element) => element.dataset.youtubeUiId === id) || null;
    },

    removeElement(id) {
      const element = this.getElement(id);
      if (element) element.remove();
      if (this._toasts[id]) delete this._toasts[id];
      return element;
    },

    _setButtonContent(button, icon, label) {
      if (icon instanceof Node) button.appendChild(icon);
      else if (icon) {
        const iconElement = document.createElement('span');
        iconElement.dataset.youtubeUiIcon = '';
        iconElement.setAttribute('aria-hidden', 'true');
        iconElement.innerHTML = String(icon);
        iconElement.style.cssText = 'display:inline-flex;align-items:center;justify-content:center';
        button.appendChild(iconElement);
      }

      if (label) {
        const labelElement = document.createElement('span');
        labelElement.textContent = label;
        button.appendChild(labelElement);
      }
    },

    setButtonIcon(button, icon) {
      const current = button?.querySelector?.('[data-youtube-ui-icon]');
      if (!current) return null;

      let replacement;
      if (icon instanceof Node) replacement = icon.cloneNode(true);
      else {
        const template = document.createElement('template');
        template.innerHTML = String(icon || '').trim();
        replacement = template.content.firstElementChild;
        if (!replacement) {
          replacement = document.createElement('span');
          replacement.textContent = String(icon || '');
        }
      }

      replacement.dataset.youtubeUiIcon = '';
      replacement.setAttribute('aria-hidden', 'true');
      if (replacement.matches?.('svg')) {
        Object.assign(replacement.style, {
          display: 'block', width: '24px', height: '24px', pointerEvents: 'none',
        });
      }
      current.replaceWith(replacement);
      this._centerButtonIcon(button);
      return replacement;
    },

    _centerButtonIcon(button) {
      const host = button?.querySelector?.('[data-youtube-ui-icon]');
      const svg = host?.matches?.('svg') ? host : host?.querySelector?.('svg');
      if (!svg?.viewBox?.baseVal) return;
      try {
        const viewBox = svg.viewBox.baseVal;
        const bounds = svg.getBBox();
        const dx = viewBox.x + (viewBox.width / 2) - (bounds.x + (bounds.width / 2));
        const dy = viewBox.y + (viewBox.height / 2) - (bounds.y + (bounds.height / 2));
        // Correct sub-pixel canvas drift, but preserve deliberately asymmetric icons.
        if (Math.abs(dx) <= 1 && Math.abs(dy) <= 1 && (Math.abs(dx) > .01 || Math.abs(dy) > .01)) {
          svg.setAttribute('viewBox', `${viewBox.x - dx} ${viewBox.y - dy} ${viewBox.width} ${viewBox.height}`);
        }
      } catch (_) {}
    },

    _lockSquareControl(element, size, options = {}) {
      if (!element?.style) return;
      const px = `${size}px`;
      const set = (property, value) => element.style.setProperty(property, value, 'important');

      // YouTube mounts additional preview styles after the first frame. Lock
      // both logical and physical dimensions so padding/min-size/hover rules
      // cannot turn a 48px control into a larger content-box later.
      set('box-sizing', 'border-box');
      set('width', px);
      set('height', px);
      set('inline-size', px);
      set('block-size', px);
      set('min-width', px);
      set('max-width', px);
      set('min-height', px);
      set('max-height', px);
      set('min-inline-size', px);
      set('max-inline-size', px);
      set('min-block-size', px);
      set('max-block-size', px);
      set('flex', `0 0 ${px}`);
      set('aspect-ratio', '1 / 1');

      if (options.button) {
        set('padding', `${options.padding ?? 0}px`);
        set('margin', '0');
        set('border', '0');
        set('border-radius', '50%');
        set('overflow', 'hidden');
        set('transform', 'none');
        set('scale', '1');
        set('font-size', '0');
        set('line-height', '0');

        const icon = element.querySelector?.('[data-youtube-ui-icon]');
        if (icon?.style) {
          const iconSize = `${options.iconSize ?? 24}px`;
          icon.style.setProperty('box-sizing', 'border-box', 'important');
          icon.style.setProperty('width', iconSize, 'important');
          icon.style.setProperty('height', iconSize, 'important');
          icon.style.setProperty('min-width', iconSize, 'important');
          icon.style.setProperty('max-width', iconSize, 'important');
          icon.style.setProperty('min-height', iconSize, 'important');
          icon.style.setProperty('max-height', iconSize, 'important');
          icon.style.setProperty('flex', `0 0 ${iconSize}`, 'important');
          icon.style.setProperty('transform', 'none', 'important');
          icon.style.setProperty('scale', '1', 'important');
        }
      }
    },

    _createButton(options = {}, baseStyle = {}) {
      const {
        id, icon = '', label = '', title = '', ariaLabel = title || label,
        className = '', style = {}, onClick,
      } = options;

      if (id && this.getElement(id)) return null;

      const button = document.createElement('button');
      button.type = 'button';
      if (id) button.dataset.youtubeUiId = id;
      if (title) button.title = title;
      if (ariaLabel) button.setAttribute('aria-label', ariaLabel);
      if (className) button.className = className;
      Object.assign(button.style, baseStyle, style);
      this._setButtonContent(button, icon, label);

      if (onClick) {
        button.addEventListener('click', (event) => {
          event.preventDefault();
          event.stopPropagation();
          onClick(event, button);
        });
      }
      return button;
    },

    _insertBeforeDirectChild(container, element, before) {
      if (!container || !element) return null;
      let anchor = before;
      while (anchor && anchor.parentNode !== container) anchor = anchor.parentNode;
      if (anchor) container.insertBefore(element, anchor);
      else container.appendChild(element);
      return element;
    },

    /** Add a native-looking control to the HTML5 player's right side. */
    addPlayerButton(options = {}) {
      const position = options.position === 'left' ? 'playerLeftControls' : 'playerRightControls';
      const container = this.findVisible(position) || this.find(position);
      if (!container) return null;

      const button = this._createButton(options, {
        width: '40px',
        height: '100%',
        padding: '0 8px',
        border: '0',
        background: 'transparent',
        color: '#fff',
        cursor: 'pointer',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        verticalAlign: 'top',
      });
      if (!button) return null;

      // ytp-button is a stable semantic player class, not an autogenerated class.
      button.classList.add('ytp-button');
      container.appendChild(button);
      return button;
    },

    /** Add a custom action beside Like/Share/Save on the watch page. */
    addEngagementButton(options = {}) {
      const container = this.findVisible('ytd-watch-metadata #actions #top-level-buttons-computed')
        || this.find('ytd-watch-metadata #actions #top-level-buttons-computed')
        || this.findVisible('ytd-watch-metadata #actions ytd-menu-renderer')
        || this.find('ytd-watch-metadata #actions ytd-menu-renderer');
      if (!container) return null;

      const button = this._createButton(options, {
        minWidth: '36px',
        height: '36px',
        padding: options.label ? '0 14px' : '0 10px',
        gap: '7px',
        border: '0',
        borderRadius: '18px',
        background: 'var(--yt-spec-badge-chip-background, rgba(255,255,255,.1))',
        color: 'var(--yt-spec-text-primary, currentColor)',
        cursor: 'pointer',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        font: '500 14px/1 Roboto, Arial, sans-serif',
        marginInline: '4px',
        whiteSpace: 'nowrap',
      });
      if (!button) return null;

      const before = this.findVisible('moreActionsButton') || this.find('moreActionsButton');
      return this._insertBeforeDirectChild(container, button, before);
    },

    /** Add a button to the right side of YouTube's top masthead. */
    addMastheadButton(options = {}) {
      const container = this.findVisible('ytd-masthead#masthead #end #buttons')
        || this.find('ytd-masthead#masthead #end #buttons')
        || this.findVisible('ytd-masthead#masthead #end')
        || this.find('ytd-masthead#masthead #end');
      if (!container) return null;

      const button = this._createButton(options, {
        minWidth: '40px',
        height: '40px',
        padding: options.label ? '0 12px' : '0 8px',
        gap: '7px',
        border: '0',
        borderRadius: '20px',
        background: 'transparent',
        color: 'var(--yt-spec-text-primary, currentColor)',
        cursor: 'pointer',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        whiteSpace: 'nowrap',
      });
      if (!button) return null;

      container.insertBefore(button, container.firstChild);
      return button;
    },

    /** Return the visible native action row in a playlist/Watch Later header. */
    getPlaylistActionRow() {
      const actionHosts = this.findAll('playlistFlexibleActions')
        .filter((element) => this.isVisible(element));

      for (const host of actionHosts) {
        const row = Array.from(host.children).find((candidate) => {
          const actions = Array.from(candidate.children).filter((child) => (
            child.querySelector?.('button, a[href]') && this.isVisible(child)
          ));
          return actions.length >= 2;
        });
        if (row) return row;
      }
      return null;
    },

    /**
     * Add an icon action to a playlist's own Download/Share/More row.
     * The live native slot is cloned, so light/dark themes and responsive
     * spacing remain YouTube's responsibility.
     */
    addPlaylistAction(options = {}) {
      const row = this.getPlaylistActionRow();
      if (!row) return null;

      const actionId = options.id || 'playlist-action';
      const existing = Array.from(row.children).find((element) => (
        element.dataset.youtubeUiPlaylistAction === actionId
      ));
      if (existing) return existing.querySelector('button');

      const nativeSlots = Array.from(row.children).filter((child) => (
        !child.dataset.youtubeUiPlaylistAction && child.querySelector?.('button')
      ));
      if (!nativeSlots.length) return null;

      // Intermediate native icon actions reserve 48px (40px button + 8px
      // separation); the final More action is 40px. Clone the first and insert
      // before the last so consecutive custom actions never stick together.
      const cloned = this._cloneNativeActionSlot(nativeSlots[0]);
      if (!cloned) return null;
      const { slot, button } = cloned;
      const before = nativeSlots[nativeSlots.length - 1];

      slot.dataset.youtubeUiId = actionId;
      slot.dataset.youtubeUiPlaylistAction = actionId;
      button.type = 'button';
      button.disabled = false;
      button.removeAttribute('aria-pressed');
      button.removeAttribute('aria-haspopup');
      button.removeAttribute('aria-expanded');
      button.removeAttribute('aria-controls');
      button.removeAttribute('command');
      button.removeAttribute('commandfor');
      button.setAttribute('aria-disabled', 'false');
      button.setAttribute('aria-label', options.ariaLabel || options.title || options.label || actionId);
      button.title = options.title || options.ariaLabel || options.label || '';

      const nativeIcon = button.querySelector('svg');
      if (nativeIcon) nativeIcon.dataset.youtubeUiIcon = '';
      if (options.icon) this.setButtonIcon(button, options.icon);

      ['pointerdown', 'pointerup', 'mousedown', 'mouseup', 'auxclick'].forEach((type) => {
        button.addEventListener(type, (event) => {
          event.preventDefault();
          event.stopPropagation();
          event.stopImmediatePropagation();
        });
      });
      if (options.onClick) {
        button.addEventListener('click', (event) => {
          event.preventDefault();
          event.stopPropagation();
          event.stopImmediatePropagation();
          options.onClick(event, button);
        });
      }

      row.insertBefore(slot, before);
      this._centerButtonIcon(button);
      return button;
    },

    /** Add a compact badge immediately after the video title. */
    addTitleBadge(options = {}) {
      const { id, text = '', title = '', style = {}, onClick } = options;
      if (id && this.getElement(id)) return null;
      const titleElement = this.getTitleElement();
      if (!titleElement?.parentNode) return null;

      const badge = document.createElement(onClick ? 'button' : 'span');
      if (onClick) badge.type = 'button';
      if (id) badge.dataset.youtubeUiId = id;
      if (title) badge.title = title;
      badge.textContent = text;
      Object.assign(badge.style, {
        display: 'inline-flex',
        alignItems: 'center',
        marginInlineStart: '8px',
        padding: '3px 7px',
        border: '0',
        borderRadius: '5px',
        background: 'var(--yt-spec-badge-chip-background, rgba(255,255,255,.1))',
        color: 'var(--yt-spec-text-secondary, currentColor)',
        font: '500 11px/1.2 Roboto, Arial, sans-serif',
        cursor: onClick ? 'pointer' : 'default',
        verticalAlign: 'middle',
        ...style,
      });
      if (onClick) badge.addEventListener('click', (event) => onClick(event, badge));
      titleElement.insertAdjacentElement('afterend', badge);
      return badge;
    },

    /** Add an action to one rendered comment's action row. */
    addCommentAction(commentElement, options = {}) {
      if (!commentElement) return null;
      const { id } = options;
      if (id && this.getElement(id)) return null;
      const container = this.find('commentActions', commentElement)
        || commentElement.querySelector?.('#action-buttons, ytd-comment-action-buttons-renderer');
      if (!container) return null;

      const button = this._createButton(options, {
        minHeight: '32px',
        padding: '0 8px',
        gap: '5px',
        border: '0',
        borderRadius: '16px',
        background: 'transparent',
        color: 'var(--yt-spec-text-secondary, currentColor)',
        cursor: 'pointer',
        display: 'inline-flex',
        alignItems: 'center',
        font: '500 12px/1 Roboto, Arial, sans-serif',
      });
      if (!button) return null;
      container.appendChild(button);
      return button;
    },

    addActionToAllComments(options = {}) {
      const comments = this.findAll('comment');
      const buttons = [];
      comments.forEach((comment, index) => {
        const baseId = options.id;
        const button = this.addCommentAction(comment, {
          ...options,
          id: baseId ? `${baseId}-${index}` : undefined,
          onClick: options.onClick
            ? (event, element) => options.onClick(comment, event, element, index)
            : undefined,
        });
        if (button) buttons.push(button);
      });
      return buttons;
    },

    // ============================================
    // Browse surfaces / video cards
    // ============================================

    getVideoEntry(target) {
      if (target?.id && target?.host && target?.link) return target;
      if (!(target instanceof Element)) return null;

      const cardSelector = this.selector('videoCard');
      const nearestCard = target.matches?.(cardSelector) ? target : target.closest?.(cardSelector);
      const richWrapper = nearestCard?.closest?.('ytd-rich-item-renderer, ytd-grid-video-renderer');
      const card = richWrapper || nearestCard;
      const scope = card || target;
      const ownLink = target.matches?.('a[href*="/watch?v="], a[href^="/shorts/"]') ? target : null;
      const link = scope.querySelector?.('a#thumbnail[href*="/watch?v="]')
        || ownLink
        || scope.querySelector?.('a[href*="/watch?v="], a[href^="/shorts/"]');
      if (!link) return null;

      const id = this.getVideoId(link.getAttribute('href'));
      if (!id) return null;
      const titleLink = scope.querySelector?.('a#video-title[href*="/watch?v="], a#video-title-link[href*="/watch?v="]')
        || scope.querySelector?.('a[href*="/watch?v="][title], a[href^="/shorts/"][title]');
      const thumbnail = scope.querySelector?.('yt-thumbnail-view-model, ytd-thumbnail')
        || link.closest?.('yt-thumbnail-view-model, ytd-thumbnail')
        || link;

      return {
        id,
        url: this.absoluteUrl(link.getAttribute('href')),
        shortUrl: `https://youtu.be/${id}`,
        title: titleLink?.getAttribute('title')
          || titleLink?.textContent?.trim()
          || link.getAttribute('aria-label')
          || '',
        element: card || target,
        card: card || target,
        link,
        titleLink,
        thumbnail,
        host: thumbnail,
      };
    },

    getVideoEntries(root = document, options = {}) {
      const cards = [];
      if (root instanceof Element && root.matches?.(this.selector('videoCard'))) cards.push(root);
      cards.push(...this.findAll('videoCard', root));

      const entries = [];
      const hosts = new Set();
      const add = (target) => {
        const entry = this.getVideoEntry(target);
        if (!entry || hosts.has(entry.host)) return;
        hosts.add(entry.host);
        entries.push(entry);
      };
      cards.forEach(add);

      // Fallback for a new experiment whose card custom element is not yet in
      // the catalog. The thumbnail id remains the structural anchor.
      if (root instanceof Element && root.matches?.(this.selector('videoThumbnailLink'))) add(root);
      this.findAll('videoThumbnailLink', root).forEach(add);

      if (!options.unique) return entries;
      const byId = new Map();
      entries.forEach((entry) => {
        if (!byId.has(entry.id)) byId.set(entry.id, entry);
      });
      return Array.from(byId.values());
    },

    getVideoCardActionRail(target) {
      const entry = this.getVideoEntry(target);
      if (!entry?.host) return null;
      return this.findAll('videoCardActionRail', entry.host)
        .find((rail) => Array.from(rail.children).some((child) => (
          !child.dataset.youtubeUiCardAction
          && (child.matches?.('button') || child.querySelector?.('button'))
        ))) || null;
    },

    _normalizeVideoCardActionOrder(actionRail) {
      if (!actionRail) return;
      const children = Array.from(actionRail.children);
      const custom = children.filter((child) => child.dataset.youtubeUiCardAction);
      const firstNative = children.find((child) => !child.dataset.youtubeUiCardAction);
      if (!firstNative) return;
      const firstNativeIndex = children.indexOf(firstNative);
      if (custom.every((child) => children.indexOf(child) < firstNativeIndex)) return;
      custom.forEach((child) => actionRail.insertBefore(child, firstNative));
    },

    _cloneNativeCardActionSlot(nativeSlot) {
      return this._cloneNativeActionSlot(nativeSlot);
    },

    _cloneNativeActionSlot(nativeSlot) {
      const nativeButton = nativeSlot?.matches?.('button')
        ? nativeSlot
        : nativeSlot?.querySelector?.('button');
      if (!nativeButton) return null;

      // Clone the whole live slot so spacing, theme tokens, focus treatment and
      // touch feedback stay owned by YouTube. DOM listeners are not cloned.
      const slot = nativeSlot.cloneNode(true);
      const button = slot.matches?.('button') ? slot : slot.querySelector('button');
      slot.removeAttribute?.('id');
      slot.querySelectorAll('[id]').forEach((element) => element.removeAttribute('id'));
      return { slot, button };
    },

    _configureVideoActionButton(button, entry, options, actionId) {
      if (!button || !entry) return null;
      button.type = 'button';
      button.disabled = false;
      button.removeAttribute('aria-pressed');
      button.removeAttribute('aria-haspopup');
      button.removeAttribute('aria-expanded');
      button.removeAttribute('aria-controls');
      button.removeAttribute('command');
      button.removeAttribute('commandfor');
      button.setAttribute('aria-disabled', 'false');
      button.setAttribute('aria-label', options.ariaLabel || options.title || options.label || actionId);
      button.title = options.title || options.ariaLabel || options.label || '';
      button.dataset.videoId = entry.id;

      const nativeIcon = button.querySelector('svg');
      if (nativeIcon) nativeIcon.dataset.youtubeUiIcon = '';
      if (options.icon) this.setButtonIcon(button, options.icon);

      ['pointerdown', 'pointerup', 'mousedown', 'mouseup', 'auxclick'].forEach((type) => {
        button.addEventListener(type, (event) => {
          event.preventDefault();
          event.stopPropagation();
          event.stopImmediatePropagation();
        });
      });
      if (options.onClick) {
        button.addEventListener('click', (event) => {
          event.preventDefault();
          event.stopPropagation();
          options.onClick(entry, event, button);
        });
      }

      this._centerButtonIcon(button);
      return button;
    },

    /** Resolve the stable native More slot and classify the card's layout. */
    getVideoCardActionHost(target) {
      const entry = this.getVideoEntry(target);
      if (!entry?.element || !entry.thumbnail) return null;

      const nativeButtons = Array.from(entry.element.querySelectorAll(
        this.selector('videoCardMenuButton'),
      )).filter((button) => !button.closest('[data-youtube-ui-card-action]'));
      const nativeButton = nativeButtons.find((button) => this.isVisible(button))
        || nativeButtons.at(-1);
      if (!nativeButton) return null;

      const nativeSlot = nativeButton.closest('button-view-model, yt-icon-button') || nativeButton;
      const parent = nativeSlot.parentElement;
      if (!parent || !entry.element.contains(parent)) return null;

      const metadata = nativeButton.closest(
        'yt-lockup-metadata-view-model, ytd-menu-renderer, #menu, #meta',
      ) || entry.element.querySelector(this.selector('videoCardMetadata')) || parent;
      const surface = entry.element.matches('yt-lockup-view-model')
        ? entry.element
        : entry.element.querySelector('yt-lockup-view-model') || entry.element;
      const thumbnailRect = entry.thumbnail.getBoundingClientRect();
      const metadataRect = metadata.getBoundingClientRect();
      const hasGeometry = thumbnailRect.width > 0 && thumbnailRect.height > 0
        && metadataRect.width > 0 && metadataRect.height > 0;
      const metadataBelow = hasGeometry
        && metadataRect.top >= thumbnailRect.bottom - Math.min(12, thumbnailRect.height * 0.08);
      const richElement = entry.element.matches('ytd-rich-item-renderer, ytd-grid-video-renderer')
        || Boolean(entry.element.closest('ytd-rich-item-renderer, ytd-grid-video-renderer'));

      return {
        entry, metadata, nativeSlot, nativeButton, parent, surface,
        layout: metadataBelow || (!hasGeometry && richElement) ? 'rich' : 'compact',
      };
    },

    _removeOtherVideoCardActions(entry, actionId, keep) {
      Array.from(entry.element.querySelectorAll('[data-youtube-ui-card-action]'))
        .filter((element) => element.dataset.youtubeUiCardAction === actionId && element !== keep)
        .forEach((element) => {
          element._youtubeUiCleanup?.();
          element.remove();
        });
    },

    /** Put a rich/grid card action in the lower end corner of the whole card. */
    addVideoCardCornerAction(target, options = {}) {
      const host = this.getVideoCardActionHost(target);
      if (!host || host.layout !== 'rich') return null;
      const { entry, nativeSlot, surface } = host;
      const actionId = options.id || 'video-card-action';
      const existing = Array.from(entry.element.querySelectorAll('[data-youtube-ui-card-corner-action]'))
        .find((element) => element.dataset.youtubeUiCardCornerAction === actionId);
      if (existing?.dataset.videoId === entry.id) {
        return existing.matches('button') ? existing : existing.querySelector('button');
      }
      if (existing) existing.remove();

      const cloned = this._cloneNativeActionSlot(nativeSlot);
      if (!cloned) return null;
      const { slot, button } = cloned;
      slot.dataset.youtubeUiCardCornerAction = actionId;
      slot.dataset.youtubeUiCardAction = actionId;
      slot.dataset.videoId = entry.id;
      Object.assign(slot.style, {
        position: 'absolute',
        insetInlineEnd: '8px',
        bottom: '8px',
        width: '40px',
        height: '40px',
        zIndex: '4',
        color: 'var(--yt-spec-text-primary, currentColor)',
      });
      Object.assign(button.style, {
        width: '40px',
        height: '40px',
        margin: '0',
        padding: '8px',
        border: '0',
        borderRadius: '50%',
        background: 'var(--yt-spec-badge-chip-background, rgba(255,255,255,.1))',
        color: 'var(--yt-spec-text-primary, currentColor)',
        cursor: 'pointer',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
      });
      this._lockSquareControl(slot, 40);
      this._lockSquareControl(button, 40, { button: true, padding: 8, iconSize: 24 });
      if (window.getComputedStyle(surface).position === 'static') {
        surface.style.position = 'relative';
      }
      surface.appendChild(slot);
      this._removeOtherVideoCardActions(entry, actionId, slot);
      return this._configureVideoActionButton(button, entry, options, actionId);
    },

    /** Put a compact/horizontal/playlist action directly before native More. */
    addVideoCardEndcapAction(target, options = {}) {
      const host = this.getVideoCardActionHost(target);
      if (!host || host.layout !== 'compact') return null;
      const { entry, nativeSlot, parent } = host;
      const actionId = options.id || 'video-card-action';
      const existing = Array.from(entry.element.querySelectorAll('[data-youtube-ui-card-endcap-action]'))
        .find((element) => element.dataset.youtubeUiCardEndcapAction === actionId);
      if (existing?.dataset.videoId === entry.id && existing.nextElementSibling === nativeSlot) {
        return existing.matches('button') ? existing : existing.querySelector('button');
      }
      if (existing) existing.remove();

      const cloned = this._cloneNativeActionSlot(nativeSlot);
      if (!cloned) return null;
      const { slot, button } = cloned;
      slot.dataset.youtubeUiCardEndcapAction = actionId;
      slot.dataset.youtubeUiCardAction = actionId;
      slot.dataset.videoId = entry.id;
      parent.insertBefore(slot, nativeSlot);
      this._removeOtherVideoCardActions(entry, actionId, slot);
      return this._configureVideoActionButton(button, entry, options, actionId);
    },

    /** Adapt one semantic action to each browse card's own object-action area. */
    addVideoCardAction(target, options = {}) {
      const host = this.getVideoCardActionHost(target);
      if (!host) return null;
      return host.layout === 'rich'
        ? this.addVideoCardCornerAction(host.entry, options)
        : this.addVideoCardEndcapAction(host.entry, options);
    },

    _rememberWatchVideoAction(actionId, options) {
      if (!this._watchVideoActions) this._watchVideoActions = new Map();
      this._watchVideoActions.set(actionId, { ...options });
      if (this._watchVideoActionTimer) return;

      // YouTube reuses and replaces the watch-page action row during SPA
      // navigation. A tiny, bounded reconciliation check is more dependable
      // (and much cheaper) than observing the whole application DOM.
      this._watchVideoActionTimer = window.setInterval(() => {
        for (const [id, rememberedOptions] of this._watchVideoActions) {
          const videoId = this.getVideoId();
          const watchPage = this.find('watchPage');
          const current = this.getElement(id);
          if (!videoId || !watchPage) {
            if (current) this.removeElement(id);
            continue;
          }
          if (current?.isConnected && current.dataset.videoId === videoId) continue;
          this.addWatchVideoAction({
            id: videoId,
            url: window.location.href,
            shortUrl: `https://youtu.be/${videoId}`,
            element: watchPage,
          }, { ...rememberedOptions, _youtubeUiReconcile: true });
        }
      }, 400);
    },

    /** Add the current video's action beside Share/Save on a watch page. */
    addWatchVideoAction(target, options = {}) {
      const liveId = this.getVideoId();
      const entry = target?.id && target.id === liveId ? target : {
        id: liveId,
        url: window.location.href,
        shortUrl: liveId ? `https://youtu.be/${liveId}` : '',
        element: this.find('watchPage'),
      };
      if (!entry.id || !entry.element) return null;
      const actionId = options.id || 'watch-video-action';
      if (!options._youtubeUiReconcile) {
        this._rememberWatchVideoAction(actionId, options);
      }
      const current = this.getElement(actionId);
      if (current?.dataset.videoId === entry.id) {
        return current.matches('button') ? current : current.querySelector('button');
      }
      if (current) this.removeElement(actionId);
      const button = this.addEngagementButton({
        ...options,
        id: actionId,
        onClick: options.onClick
          ? (event, element) => {
            const currentId = this.getVideoId();
            const liveEntry = currentId ? {
              ...entry,
              id: currentId,
              url: window.location.href,
              shortUrl: `https://youtu.be/${currentId}`,
              element: this.find('watchPage') || entry.element,
            } : entry;
            options.onClick(liveEntry, event, element);
          }
          : undefined,
      });
      if (button) button.dataset.videoId = entry.id;
      return button;
    },

    getShortsEntry(target = null) {
      if (target?.id && target?.element?.matches?.(this.selector('shortsRenderer'))) return target;
      const requestedId = target?.id || this.getVideoId();
      const renderers = target instanceof Element
        ? [target.closest(this.selector('shortsRenderer'))].filter(Boolean)
        : this.findAll('shortsRenderer').filter((renderer) => this.isVisible(renderer));
      for (const renderer of renderers) {
        const link = renderer.querySelector('a[href^="/shorts/"]');
        const id = this.getVideoId(link?.getAttribute('href')) || requestedId;
        if (!id || (requestedId && id !== requestedId)) continue;
        return {
          id,
          url: this.absoluteUrl(link?.getAttribute('href')) || window.location.href,
          shortUrl: `https://youtu.be/${id}`,
          element: renderer,
          host: renderer,
          link,
        };
      }
      return null;
    },

    /** Clone Share's native Shorts slot and place the action directly after it. */
    addShortsAction(target, options = {}) {
      const entry = this.getShortsEntry(target);
      if (!entry) return null;
      const rail = entry.element.querySelector(this.selector('shortsActionRail'));
      const shareButton = rail?.querySelector('button[aria-label="Share"]');
      if (!rail || !shareButton) return null;
      let shareSlot = shareButton;
      while (shareSlot.parentElement && shareSlot.parentElement !== rail) {
        shareSlot = shareSlot.parentElement;
      }
      if (shareSlot.parentElement !== rail) return null;

      const actionId = options.id || 'shorts-action';
      const existing = Array.from(rail.children)
        .find((element) => element.dataset.youtubeUiShortsAction === actionId);
      if (existing?.dataset.videoId === entry.id && existing.previousElementSibling === shareSlot) {
        return existing.matches('button') ? existing : existing.querySelector('button');
      }
      if (existing) existing.remove();

      const cloned = this._cloneNativeActionSlot(shareSlot);
      if (!cloned) return null;
      const { slot, button } = cloned;
      slot.dataset.youtubeUiShortsAction = actionId;
      slot.dataset.videoId = entry.id;
      Array.from(slot.querySelectorAll('*')).forEach((element) => {
        if (!element.children.length && element.textContent.trim() === 'Share') {
          element.textContent = options.label || 'Copy';
        }
      });
      rail.insertBefore(slot, shareSlot.nextSibling);
      return this._configureVideoActionButton(button, entry, options, actionId);
    },

    addActionToAllVideoCards(options = {}, root = document) {
      return this.getVideoEntries(root)
        .map((entry) => this.addVideoCardAction(entry, options))
        .filter(Boolean);
    },

    /** Resolve the video object currently under a pointer or keyboard focus. */
    getVideoTargetEntry(target) {
      if (!(target instanceof Element)) return null;

      const card = target.closest(this.selector('videoCard'));
      if (card) return this.getVideoEntry(card);

      const preview = target.closest(this.selector('videoPreview'));
      if (preview) return this.getVideoPreviewEntry(preview);

      const player = target.closest(`${this.selector('playerHost')}, ${this.selector('player')}`);
      const id = player ? this.getVideoId() : null;
      if (!id) return null;
      return {
        id,
        url: window.location.href,
        shortUrl: `https://youtu.be/${id}`,
        title: this.getTitle(),
        element: player,
        host: player,
        link: null,
      };
    },

    /** Resolve the visual surface that should acknowledge a video action. */
    getVideoFeedbackTarget(target) {
      const entry = target?.id ? target : this.getVideoTargetEntry(target);
      let element = entry?.element || entry?.host;
      if (!(element instanceof Element)) return null;

      const watchPage = element.matches?.(this.selector('watchPage'))
        ? element
        : element.closest?.(this.selector('watchPage'));
      if (watchPage && (element === watchPage || element === document.documentElement)) {
        return this.findVisible('playerHost', watchPage)
          || this.findVisible('player', watchPage)
          || watchPage;
      }

      return element.closest?.(this.selector('videoPreview'))
        || element.closest?.(`${this.selector('playerHost')}, ${this.selector('player')}`)
        || element.closest?.(this.selector('shortsRenderer'))
        || element.closest?.(this.selector('videoCard'))
        || element;
    },

    /** Briefly ring the exact video surface after a successful action. */
    flashVideoTarget(target, options = {}) {
      const surface = this.getVideoFeedbackTarget(target);
      if (!surface) return null;

      const computed = window.getComputedStyle(surface);
      const accent = options.color
        || window.getComputedStyle(document.documentElement)
          .getPropertyValue('--yt-spec-call-to-action').trim()
        || '#3ea6ff';
      const restingShadow = computed.boxShadow === 'none'
        ? '0 0 0 0 transparent'
        : computed.boxShadow;
      const activeShadow = computed.boxShadow === 'none'
        ? `0 0 0 3px ${accent}`
        : `${computed.boxShadow}, 0 0 0 3px ${accent}`;
      const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
      const duration = reducedMotion ? 150 : (options.duration || 460);

      surface._youtubeUiFeedbackAnimation?.cancel?.();
      surface.dataset.youtubeUiFeedback = options.state || 'success';

      if (typeof surface.animate !== 'function') {
        const previous = surface.style.boxShadow;
        surface.style.boxShadow = activeShadow;
        setTimeout(() => {
          surface.style.boxShadow = previous;
          delete surface.dataset.youtubeUiFeedback;
        }, duration);
        return surface;
      }

      const animation = surface.animate([
        { boxShadow: restingShadow, offset: 0 },
        { boxShadow: activeShadow, offset: reducedMotion ? 0.15 : 0.18 },
        { boxShadow: activeShadow, offset: reducedMotion ? 0.7 : 0.42 },
        { boxShadow: restingShadow, offset: 1 },
      ], {
        duration,
        easing: 'cubic-bezier(0.16, 1, 0.3, 1)',
      });
      surface._youtubeUiFeedbackAnimation = animation;
      const cleanUp = () => {
        if (surface._youtubeUiFeedbackAnimation !== animation) return;
        delete surface._youtubeUiFeedbackAnimation;
        delete surface.dataset.youtubeUiFeedback;
      };
      animation.addEventListener('finish', cleanUp, { once: true });
      animation.addEventListener('cancel', cleanUp, { once: true });
      return surface;
    },

    /** Keep a subtle, non-layout-shifting ring on the video under the pointer. */
    setVideoTargetHovered(target, active = true) {
      const next = active ? this.getVideoFeedbackTarget(target) : null;
      const previous = this._hoveredVideoSurface;
      if (previous && previous !== next) {
        const saved = previous._youtubeUiHoverStyle;
        if (saved) {
          for (const [property, value, priority] of saved) {
            if (value) previous.style.setProperty(property, value, priority);
            else previous.style.removeProperty(property);
          }
        }
        delete previous._youtubeUiHoverStyle;
        delete previous.dataset.youtubeUiHovered;
        this._hoveredVideoSurface = null;
      }
      if (!next || next === previous) return next;

      const accent = window.getComputedStyle(document.documentElement)
        .getPropertyValue('--yt-spec-call-to-action').trim() || '#3ea6ff';
      next._youtubeUiHoverStyle = ['outline', 'outline-offset'].map((property) => [
        property,
        next.style.getPropertyValue(property),
        next.style.getPropertyPriority(property),
      ]);
      next.style.setProperty('outline', `2px solid ${accent}`, 'important');
      next.style.setProperty('outline-offset', '-2px', 'important');
      next.dataset.youtubeUiHovered = 'true';
      this._hoveredVideoSurface = next;
      return next;
    },

    /** Track one semantic video under the pointer without per-card listeners. */
    onVideoHover(callback, options = {}) {
      const root = this.find('app') || document.documentElement;
      let current = null;
      const sameEntry = (left, right) => Boolean(left && right
        && left.id === right.id && (left.host || left.element) === (right.host || right.element));
      const onPointerOver = (event) => {
        const entry = this.getVideoTargetEntry(event.target);
        if (!entry || sameEntry(current, entry)) return;
        current = entry;
        if (options.highlight) this.setVideoTargetHovered(entry, true);
        callback(entry, event);
      };
      const onPointerOut = (event) => {
        if (!current) return;
        const next = this.getVideoTargetEntry(event.relatedTarget);
        if (sameEntry(current, next)) return;
        if (options.highlight) this.setVideoTargetHovered(current, false);
        current = null;
        callback(null, event);
      };
      const reset = () => {
        if (!current) return;
        if (options.highlight) this.setVideoTargetHovered(current, false);
        current = null;
        callback(null, null);
      };

      root.addEventListener('pointerover', onPointerOver, true);
      root.addEventListener('pointerout', onPointerOut, true);
      window.addEventListener('yt-navigate-finish', reset);
      return () => {
        root.removeEventListener('pointerover', onPointerOver, true);
        root.removeEventListener('pointerout', onPointerOut, true);
        window.removeEventListener('yt-navigate-finish', reset);
        if (options.highlight) this.setVideoTargetHovered(current, false);
      };
    },

    getVideoPreviews(root = document) {
      const previews = [];
      if (root instanceof Element && root.matches?.(this.selector('videoPreview'))) previews.push(root);
      previews.push(...this.findAll('videoPreview', root));
      return previews.filter((preview, index, all) => all.indexOf(preview) === index);
    },

    /** Resolve a portal preview from its own stable watch link. */
    getVideoPreviewEntry(target = null) {
      if (target?.preview && target?.id) return target;

      const requestedId = target?.id || null;
      let previews;
      if (target instanceof Element) {
        const selector = this.selector('videoPreview');
        const preview = target.matches?.(selector) ? target : target.closest?.(selector);
        previews = preview ? [preview] : [];
      } else {
        previews = this.getVideoPreviews().filter((preview) => this.isVisible(preview));
      }

      for (const preview of previews) {
        if (!this.isVisible(preview)) continue;
        const actionRail = preview.querySelector(this.selector('videoPreviewActionRail'));
        const controls = preview.querySelector(this.selector('videoPreviewControls'));
        const previewLink = preview.querySelector('#media-container-link[href*="/watch?v="], a[href*="/watch?v="]');
        const previewId = this.getVideoId(previewLink?.getAttribute?.('href'));
        const id = previewId || requestedId;
        if (!id || (requestedId && previewId && requestedId !== previewId)) continue;

        return {
          id,
          url: this.absoluteUrl(previewLink?.getAttribute?.('href')),
          shortUrl: `https://youtu.be/${id}`,
          title: previewLink?.getAttribute?.('aria-label') || '',
          element: preview,
          card: null,
          link: previewLink,
          titleLink: null,
          thumbnail: preview,
          host: preview,
          preview,
          controls,
          actionRail,
        };
      }
      return null;
    },

    _layoutVideoPreviewActions(entry) {
      if (!entry?.actionRail) return;

      // Undo the former overlay implementation if a page survived a hot
      // reload. The native rail owns position, spacing, size and theme.
      entry.preview.querySelectorAll('[data-youtube-ui-preview-action]').forEach((slot) => {
        if (!entry.actionRail.contains(slot)) slot.remove();
      });
      entry.actionRail.querySelectorAll('[data-youtube-ui-preview-action-spacer]')
        .forEach((element) => element.remove());
      entry.actionRail.style.paddingBlockStart = '';
      entry.actionRail.style.translate = '';

      const children = Array.from(entry.actionRail.children);
      const custom = children.filter((child) => child.dataset.youtubeUiPreviewAction);
      const firstNative = children.find((child) => !child.dataset.youtubeUiPreviewAction);
      if (!firstNative) return;
      const firstNativeIndex = children.indexOf(firstNative);
      if (custom.every((slot) => children.indexOf(slot) < firstNativeIndex)) return;
      custom.forEach((slot) => entry.actionRail.insertBefore(slot, firstNative));
    },

    /** Clone an action into YouTube's live Mute/CC preview rail. */
    addVideoPreviewAction(target, options = {}) {
      const entry = this.getVideoPreviewEntry(target);
      if (!entry?.preview || !entry.actionRail) return null;

      const actionId = options.id || 'video-preview-action';
      entry.preview.querySelectorAll('[data-youtube-ui-preview-action]').forEach((slot) => {
        if (!entry.actionRail.contains(slot)) slot.remove();
      });

      let existing = Array.from(entry.actionRail.children)
        .find((element) => element.dataset.youtubeUiPreviewAction === actionId);
      let button = existing?.matches?.('button') ? existing : existing?.querySelector('button');
      if (existing?.dataset.videoId === entry.id && button) {
        this._layoutVideoPreviewActions(entry);
        return button;
      }
      if (existing) existing.remove();

      const nativeButton = entry.actionRail.querySelector('button');
      const nativeSlot = Array.from(entry.actionRail.children)
        .find((child) => !child.dataset.youtubeUiPreviewAction && (
          child === nativeButton || child.contains(nativeButton)
        ));
      const cloned = this._cloneNativeActionSlot(nativeSlot || nativeButton);
      if (!cloned) return null;
      const { slot } = cloned;
      button = cloned.button;
      slot.dataset.youtubeUiPreviewAction = actionId;
      slot.dataset.videoId = entry.id;
      entry.actionRail.insertBefore(slot, entry.actionRail.firstElementChild);

      this._configureVideoActionButton(button, entry, options, actionId);
      this._layoutVideoPreviewActions(entry);
      return button;
    },

    /**
     * Mount on the one card the user actually enters. If YouTube has not
     * mounted its native action rail yet, observe only that card and stop as
     * soon as the callback succeeds (or after a short timeout).
     */
    onVideoCards(callback, options = {}) {
      const root = this.find('app') || document.documentElement;
      const seen = new WeakMap();
      const pending = new Map();
      let stopped = false;

      const stopPending = (host) => {
        const task = pending.get(host);
        if (!task) return;
        task.observer.disconnect();
        clearTimeout(task.timer);
        pending.delete(host);
      };
      const attempt = (target) => {
        if (stopped) return true;
        const entry = target?.id && target?.host ? target : this.getVideoEntry(target);
        if (!entry) return false;
        if (!options.repeat && seen.get(entry.host) === entry.id) return true;
        const result = callback(entry);
        const mounted = Boolean(result) || Array.from(entry.element.querySelectorAll(
          '[data-youtube-ui-card-action]',
        )).some((slot) => slot.dataset.videoId === entry.id);
        if (mounted) seen.set(entry.host, entry.id);
        return mounted;
      };
      const watch = (target) => {
        const entry = this.getVideoEntry(target);
        if (!entry || attempt(entry) || pending.has(entry.host)) return;

        let queued = false;
        const retry = () => {
          if (queued || stopped) return;
          queued = true;
          queueMicrotask(() => {
            queued = false;
            if (attempt(entry)) stopPending(entry.host);
          });
        };
        const observer = new MutationObserver(retry);
        observer.observe(entry.element, { childList: true, subtree: true });
        const timer = setTimeout(() => stopPending(entry.host), options.timeoutMs ?? 1600);
        pending.set(entry.host, { observer, timer });
      };
      const onEnter = (event) => {
        if (!(event.target instanceof Element)) return;
        const card = event.target.closest(this.selector('videoCard'));
        if (card) watch(card);
      };
      const reset = () => Array.from(pending.keys()).forEach(stopPending);

      root.addEventListener('pointerover', onEnter, true);
      root.addEventListener('focusin', onEnter, true);
      window.addEventListener('yt-navigate-finish', reset);
      if (options.initial === true) this.getVideoEntries(document).forEach(watch);

      return () => {
        stopped = true;
        reset();
        root.removeEventListener('pointerover', onEnter, true);
        root.removeEventListener('focusin', onEnter, true);
        window.removeEventListener('yt-navigate-finish', reset);
      };
    },

    /**
     * Retry only the requested preview for a bounded period. No document-wide
     * MutationObserver is used, and the portal identifies itself by watch URL.
     */
    onVideoPreviews(callback, options = {}) {
      const root = this.find('app') || document.documentElement;
      const seen = new WeakMap();
      const pending = new Map();
      let stopped = false;

      const stopPending = (key) => {
        const task = pending.get(key);
        if (!task) return;
        clearInterval(task.interval);
        clearTimeout(task.timer);
        pending.delete(key);
      };
      const attempt = (target) => {
        if (stopped) return true;
        const entry = this.getVideoPreviewEntry(target);
        if (!entry?.actionRail) return false;
        if (!options.repeat && seen.get(entry.preview) === entry.id) return true;
        const result = callback(entry);
        const mounted = Boolean(result) || Array.from(entry.actionRail.children)
          .some((slot) => slot.dataset.youtubeUiPreviewAction && slot.dataset.videoId === entry.id);
        if (mounted) seen.set(entry.preview, entry.id);
        return mounted;
      };
      const watch = (target) => {
        const key = target?.id || target;
        if (!key || attempt(target) || pending.has(key)) return;
        const interval = setInterval(() => {
          if (attempt(target)) stopPending(key);
        }, options.retryMs ?? 50);
        const timer = setTimeout(() => stopPending(key), options.timeoutMs ?? 1600);
        pending.set(key, { interval, timer });
      };
      const onEnter = (event) => {
        if (!(event.target instanceof Element)) return;
        const preview = event.target.closest(this.selector('videoPreview'));
        if (preview) {
          watch(preview);
          return;
        }
        const card = event.target.closest(this.selector('videoCard'));
        const entry = card ? this.getVideoEntry(card) : null;
        if (entry) watch({ id: entry.id });
      };
      const reset = () => Array.from(pending.keys()).forEach(stopPending);

      root.addEventListener('pointerover', onEnter, true);
      root.addEventListener('focusin', onEnter, true);
      window.addEventListener('yt-navigate-finish', reset);
      if (options.initial === true) this.getVideoPreviews()
        .filter((preview) => this.isVisible(preview)).forEach(watch);

      return () => {
        stopped = true;
        reset();
        root.removeEventListener('pointerover', onEnter, true);
        root.removeEventListener('focusin', onEnter, true);
        window.removeEventListener('yt-navigate-finish', reset);
      };
    },

    showToast(text, options = {}) {
      const { id = 'youtube-ui-toast', duration = 2500, style = {} } = options;
      let toast = this._toasts[id] || this.getElement(id);
      if (!toast) {
        toast = document.createElement('div');
        toast.dataset.youtubeUiId = id;
        toast.setAttribute('role', 'status');
        Object.assign(toast.style, {
          position: 'fixed',
          left: '50%',
          bottom: '28px',
          zIndex: '2147483647',
          transform: 'translate(-50%, 12px)',
          opacity: '0',
          padding: '10px 16px',
          borderRadius: '8px',
          background: 'rgba(15,15,15,.96)',
          color: '#fff',
          font: '500 14px/1.35 Roboto, Arial, sans-serif',
          boxShadow: '0 6px 24px rgba(0,0,0,.35)',
          transition: 'opacity .18s ease, transform .18s ease',
          pointerEvents: 'none',
          ...style,
        });
        (document.body || document.documentElement).appendChild(toast);
        this._toasts[id] = toast;
      }

      toast.textContent = text;
      requestAnimationFrame(() => {
        toast.style.opacity = '1';
        toast.style.transform = 'translate(-50%, 0)';
      });
      clearTimeout(toast._youtubeUiTimer);
      if (duration > 0) {
        toast._youtubeUiTimer = setTimeout(() => this.hideToast(id), duration);
      }
      return toast;
    },

    hideToast(id = 'youtube-ui-toast') {
      const toast = this._toasts[id] || this.getElement(id);
      if (!toast) return null;
      toast.style.opacity = '0';
      toast.style.transform = 'translate(-50%, 12px)';
      setTimeout(() => {
        toast.remove();
        delete this._toasts[id];
      }, 220);
      return toast;
    },

    getVideoId(url = window.location.href) {
      try {
        const parsed = new URL(url, window.location.origin);
        if (parsed.pathname === '/watch') return parsed.searchParams.get('v');
        if (parsed.hostname === 'youtu.be') return parsed.pathname.slice(1) || null;
        if (parsed.pathname.startsWith('/shorts/')) return parsed.pathname.split('/')[2] || null;
      } catch (_) {
        // Invalid URLs are simply not YouTube videos.
      }
      return null;
    },

    getPlayer() {
      return this.findVisible('player') || this.find('player');
    },

    getVideo() {
      return this.findVisible('video') || this.find('video');
    },

    play() {
      return this.getVideo()?.play() || null;
    },

    pause() {
      const video = this.getVideo();
      if (video) video.pause();
      return video;
    },

    seekTo(seconds) {
      const video = this.getVideo();
      if (!video || !Number.isFinite(Number(seconds))) return null;
      video.currentTime = Math.max(0, Number(seconds));
      return video.currentTime;
    },

    getTitleElement() {
      return this.findVisible('title') || this.find('title');
    },

    getTitle() {
      return this.getTitleElement()?.textContent?.trim() || '';
    },

    getChannel() {
      const element = this.findVisible('owner') || this.find('owner');
      const link = element?.querySelector?.('#channel-name a[href]')
        || element?.querySelector?.('a[href^="/channel/"], a[href^="/@"]')
        || this.findVisible('channelLink')
        || this.find('channelLink');
      const subscribers = element?.querySelector?.('#owner-sub-count')?.textContent?.trim() || '';

      return {
        element,
        link,
        name: link?.textContent?.trim() || '',
        url: this.absoluteUrl(link?.getAttribute?.('href')),
        subscribers,
        subscribeButton: this.getSubscribeButton(),
      };
    },

    getSubscribeButton() {
      return this.findVisible('subscribeButton') || this.find('subscribeButton');
    },

    getDescriptionElement() {
      return this.findVisible('description') || this.find('description');
    },

    getDescription() {
      const element = this.getDescriptionElement();
      if (!element) return { element: null, text: '', expanded: false };

      const visibleText = this.findVisible('#attributed-snippet-text, #snippet-text', element);
      const expanded = element.hasAttribute('is-expanded')
        || !!this.findVisible('#collapse[role="button"]', element);

      return {
        element,
        text: (visibleText || element).textContent?.trim() || '',
        expanded,
      };
    },

    expandDescription() {
      const button = this.findVisible('descriptionExpand');
      button?.click();
      return button;
    },

    collapseDescription() {
      const button = this.findVisible('descriptionCollapse');
      button?.click();
      return button;
    },

    getEngagementControls() {
      return {
        element: this.findVisible('engagementControls') || this.find('engagementControls'),
        like: this.findVisible('likeButton') || this.find('likeButton'),
        dislike: this.findVisible('dislikeButton') || this.find('dislikeButton'),
        share: this.findVisible('shareButton') || this.find('shareButton'),
        save: this.findVisible('saveButton') || this.find('saveButton'),
        more: this.findVisible('moreActionsButton') || this.find('moreActionsButton'),
      };
    },

    getComments() {
      return this.find('comments');
    },

    getCommentThreads() {
      return this.findAll('commentThread');
    },

    getCommentComposer() {
      return this.findVisible('commentComposer') || this.find('commentComposer');
    },

    getCommentInput() {
      const composer = this.getCommentComposer();
      if (!composer) return null;
      return this.findVisible('#contenteditable-root[contenteditable="true"], #simplebox-placeholder', composer)
        || this.find('#contenteditable-root[contenteditable="true"], #simplebox-placeholder', composer);
    },

    getTranscript() {
      const panel = this.find('transcriptPanel');
      const segments = this.findAll('transcriptSegment', panel || document);
      return {
        section: this.findVisible('transcriptSection') || this.find('transcriptSection'),
        button: this.findVisible('transcriptButton') || this.find('transcriptButton'),
        panel,
        segments,
        text: segments.map((segment) => segment.textContent?.trim()).filter(Boolean).join('\n'),
        open: this.isVisible(panel),
      };
    },

    showTranscript() {
      const transcript = this.getTranscript();
      if (!transcript.open) transcript.button?.click();
      return transcript.panel || transcript.button;
    },

    getChapters() {
      const panel = this.find('chaptersPanel');
      const root = panel || this.find('chaptersSection') || document;
      const items = this.findAll('chapterItem', root);

      return {
        section: this.findVisible('chaptersSection') || this.find('chaptersSection'),
        panel,
        items,
        chapters: items.map((item) => ({
          element: item,
          title: item.querySelector('#title')?.textContent?.trim() || '',
          timestamp: item.querySelector('#time')?.textContent?.trim() || '',
        })),
        open: this.isVisible(panel),
      };
    },

    getNavigation() {
      return {
        masthead: this.findVisible('masthead') || this.find('masthead'),
        home: this.findVisible('homeLink') || this.find('homeLink'),
        guideButton: this.findVisible('guideButton') || this.find('guideButton'),
        guide: this.findVisible('navigation') || this.find('navigation'),
        searchBox: this.findVisible('searchBox') || this.find('searchBox'),
        searchInput: this.getSearchInput(),
        searchButton: this.findVisible('searchButton') || this.find('searchButton'),
      };
    },

    getSearchInput() {
      return this.findVisible('searchInput') || this.find('searchInput');
    },

    search(query) {
      const input = this.getSearchInput();
      if (!input) return false;

      const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
      if (valueSetter) valueSetter.call(input, String(query));
      else input.value = String(query);

      input.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
      input.dispatchEvent(new Event('change', { bubbles: true, composed: true }));

      const button = this.findVisible('searchButton') || this.find('searchButton');
      if (button) button.click();
      else input.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Enter', code: 'Enter', bubbles: true, composed: true,
      }));
      return true;
    },

    getCurrentVideo() {
      return {
        id: this.getVideoId(),
        url: window.location.href,
        title: this.getTitle(),
        player: this.getPlayer(),
        video: this.getVideo(),
        channel: this.getChannel(),
        description: this.getDescription(),
        engagement: this.getEngagementControls(),
      };
    },

    whenReady(nameOrSelector, options = {}) {
      const { timeout = 10000, visible = false } = options;
      const find = () => visible
        ? this.findVisible(nameOrSelector)
        : this.find(nameOrSelector);
      const immediate = find();
      if (immediate) return Promise.resolve(immediate);

      return new Promise((resolve) => {
        let done = false;
        const finish = (value) => {
          if (done) return;
          done = true;
          observer.disconnect();
          clearTimeout(timer);
          resolve(value);
        };
        const observer = new MutationObserver(() => {
          const element = find();
          if (element) finish(element);
        });
        const timer = setTimeout(() => finish(null), timeout);
        observer.observe(document.documentElement, { childList: true, subtree: true });
      });
    },

    onNewComment(callback) {
      const root = this.getComments() || document.documentElement;
      const selector = this.selector('comment');
      const observer = new MutationObserver((mutations) => {
        mutations.forEach((mutation) => {
          mutation.addedNodes.forEach((node) => {
            if (node.nodeType !== Node.ELEMENT_NODE) return;
            const comments = node.matches?.(selector)
              ? [node]
              : Array.from(node.querySelectorAll?.(selector) || []);
            comments.forEach((comment) => callback(comment));
          });
        });
      });
      observer.observe(root, { childList: true, subtree: true });
      return observer;
    },

    onVideoChange(callback) {
      let previousId = null;
      let scheduled = false;

      const emit = () => {
        scheduled = false;
        const id = this.getVideoId();
        if (!id || id === previousId) return;
        previousId = id;
        callback(this.getCurrentVideo());
      };
      const schedule = () => {
        if (scheduled) return;
        scheduled = true;
        queueMicrotask(emit);
      };

      window.addEventListener('yt-navigate-finish', schedule);
      window.addEventListener('yt-page-data-updated', schedule);
      const observer = new MutationObserver(schedule);
      const title = document.querySelector('title');
      observer.observe(title || document.documentElement, {
        childList: true,
        characterData: true,
        subtree: true,
      });
      schedule();

      return () => {
        window.removeEventListener('yt-navigate-finish', schedule);
        window.removeEventListener('yt-page-data-updated', schedule);
        observer.disconnect();
      };
    },

    inspectAnchors(names = this.debugAnchorNames) {
      const anchors = {};
      names.forEach((name) => {
        const elements = this.findAll(name);
        const visible = elements.filter((element) => this.isVisible(element));
        const first = visible[0] || elements[0] || null;
        anchors[name] = {
          selector: this.selectors[name],
          count: elements.length,
          visibleCount: visible.length,
          found: elements.length > 0,
          first,
          tag: first?.tagName?.toLowerCase() || null,
          text: first?.textContent?.replace(/\s+/g, ' ')?.trim()?.slice(0, 80) || '',
        };
      });
      return anchors;
    },

    removeDebugOverlay() {
      document.querySelector('[data-youtube-ui-debug]')?.remove();
    },

    debugAnchors(options = {}) {
      const { overlay = true, log = true, names = this.debugAnchorNames } = options;
      const anchors = this.inspectAnchors(names);
      const found = Object.values(anchors).filter((anchor) => anchor.found).length;
      const result = { ok: found === names.length, found, total: names.length, anchors };

      if (log && console?.table) {
        console.table(Object.fromEntries(Object.entries(anchors).map(([name, anchor]) => [name, {
          found: anchor.found,
          count: anchor.count,
          visible: anchor.visibleCount,
          tag: anchor.tag,
          selector: anchor.selector,
        }])));
      }

      if (overlay) {
        this.removeDebugOverlay();
        const panel = document.createElement('aside');
        panel.dataset.youtubeUiDebug = '';
        panel.setAttribute('role', 'status');
        panel.style.cssText = [
          'position:fixed', 'right:16px', 'bottom:16px', 'z-index:2147483647',
          'width:min(420px,calc(100vw - 32px))', 'max-height:70vh', 'overflow:auto',
          'padding:14px', 'border-radius:10px', 'background:rgba(15,15,15,.96)',
          'color:#fff', 'font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace',
          'box-shadow:0 8px 32px rgba(0,0,0,.45)',
        ].join(';');

        const title = document.createElement('strong');
        title.textContent = `YouTubeUI anchors: ${found}/${names.length}`;
        title.style.cssText = 'display:block;margin-bottom:8px;font-size:13px';
        panel.appendChild(title);

        Object.entries(anchors).forEach(([name, anchor]) => {
          const row = document.createElement('div');
          row.textContent = `${anchor.found ? '●' : '○'} ${name}  ${anchor.visibleCount}/${anchor.count}`;
          row.title = anchor.selector;
          row.style.color = anchor.found ? '#6ee7b7' : '#fca5a5';
          panel.appendChild(row);
        });

        const close = document.createElement('button');
        close.type = 'button';
        close.textContent = 'Close';
        close.style.cssText = 'margin-top:10px;border:0;border-radius:6px;padding:5px 9px;cursor:pointer';
        close.addEventListener('click', () => panel.remove());
        panel.appendChild(close);
        (document.body || document.documentElement).appendChild(panel);
        result.overlay = panel;
      }

      return result;
    },

    smokeTest(options = {}) {
      return this.debugAnchors(options);
    },
  };

  window.YouTubeUI = YouTubeUI;
})();
