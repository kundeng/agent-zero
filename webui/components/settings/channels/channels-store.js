/**
 * Channels Settings — Alpine store (spec 08 1.18).
 *
 * Mirrors the shape of `mcp-servers-store.js`. Owns:
 *   - the list of provisioner-registered channels + their status,
 *   - status polling (15s) while the modal is open,
 *   - the active wizard's step machine (which step, inputs, errors),
 *   - the OAuth-callback `postMessage` listener (spec 08 D3 auto-capture).
 *
 * The store knows nothing platform-specific. It dispatches every call
 * through the generic `/channels_*` endpoints; the per-platform
 * wizard descriptors come back from `/channels_wizard?channel_type=<x>`
 * and drive the entire UI for any platform.
 */

import { createStore } from "/js/AlpineStore.js";
import sleep from "/js/sleep.js";
import * as API from "/js/api.js";

const STATUS_POLL_MS = 15000;
const OAUTH_TIMEOUT_FALLBACK_MS_DEFAULT = 90_000;

const model = {
  // Status list (one entry per registered provisioner).
  channels: [],
  loading: true,
  statusCheck: false,

  // Active wizard state. `null` when no wizard is open.
  wizard: null,

  // Last test-connection result keyed by channel_type.
  testResults: {},

  // ------------------------------------------------------------------
  // Lifecycle
  // ------------------------------------------------------------------

  async initialize() {
    this._installOauthListener();
    this.startStatusCheck();
  },

  onClose() {
    this.stopStatusCheck();
    this._removeOauthListener();
    this.wizard = null;
  },

  // ------------------------------------------------------------------
  // Status polling
  // ------------------------------------------------------------------

  async startStatusCheck() {
    this.statusCheck = true;
    let firstLoad = true;
    while (this.statusCheck) {
      await this._statusCheck();
      if (firstLoad) {
        this.loading = false;
        firstLoad = false;
      }
      await sleep(STATUS_POLL_MS);
    }
  },

  async stopStatusCheck() {
    this.statusCheck = false;
  },

  async _statusCheck() {
    try {
      const resp = await API.callJsonApi("channels_status", {});
      if (resp.success) {
        // Stable order: alphabetic by channel_type, then bot_name. Sort
        // by bot_name secondarily so the "Add another bot" button lands
        // after every existing bot of a platform (see isLastOfPlatform).
        this.channels = (resp.channels || []).slice().sort((a, b) => {
          const t = a.channel_type.localeCompare(b.channel_type);
          if (t !== 0) return t;
          return (a.bot_name || "").localeCompare(b.bot_name || "");
        });
      }
    } catch (e) {
      console.error("channels_status failed:", e);
    }
  },

  // ------------------------------------------------------------------
  // Per-card helpers (spec 09 task 1.12)
  // ------------------------------------------------------------------

  /**
   * Composite key for a channel card — same shape as the (channel_type,
   * bot_name) ThreadStore key. Used as `:key` in x-for and as the
   * lookup key into `testResults`.
   */
  cardKey(ch) {
    return `${ch.channel_type}:${ch.bot_name || ""}`;
  },

  /**
   * True iff `ch` is the last card of its platform (in the sorted
   * `channels` array). Drives placement of the per-platform
   * "+ Add another bot" button.
   */
  isLastOfPlatform(idx) {
    const next = this.channels[idx + 1];
    return !next || next.channel_type !== this.channels[idx].channel_type;
  },

  /**
   * True iff the card's bot is the pre-spec-09 ``_legacy`` constant.
   * Only those hide the bot_name suffix — migrated installs end up
   * with bot_name="default" and SHOULD surface that label so the
   * operator can see the entity that exists and reason about adding
   * a second bot beside it.
   */
  isLegacyBot(ch) {
    return ch.bot_name === "_legacy";
  },

  /**
   * True iff this card is the first real bot of its platform. The
   * Test / Default-project controls are hidden on additional bots
   * because the server endpoints behind them
   * (/channels_test, /channels_bind_project) still key by
   * channel_type only — leaving them visible would let an operator
   * accidentally apply a binding meant for bot #2 to bot #1.
   */
  isFirstBotOfPlatform(idx) {
    const ch = this.channels[idx];
    if (!ch || !ch.bot_name) return false;
    const prev = this.channels[idx - 1];
    return !prev || prev.channel_type !== ch.channel_type;
  },

  /**
   * True iff every entry's bot_name is empty (placeholder) or a
   * legacy name — i.e. no operator has ever set up a named bot. The
   * Channels tab uses this to show the spec 09 D4 first-run banner.
   */
  hasNoRealBots() {
    if (!this.channels.length) return true;
    // Placeholder rows have bot_name === ""; pre-spec-09 adapters
    // report bot_name === "_legacy". Neither counts as a real bot.
    // bot_name === "default" IS a real (migrated) bot — operators
    // should see it instead of the first-run banner.
    return this.channels.every(
      (c) => !c.bot_name || c.bot_name === "_legacy"
    );
  },

  // ------------------------------------------------------------------
  // Wizard
  // ------------------------------------------------------------------

  /**
   * Open the wizard for `channelType`. Fetches the step descriptor
   * list from `/channels_wizard` and starts at step 0.
   */
  async startWizard(channelType) {
    this.wizard = {
      channelType,
      sessionId: null,
      steps: [],
      currentStepIdx: 0,
      currentStep: null,
      inputs: {},
      result: null,
      error: null,
      busy: false,
      // Set when waiting for OAuth callback so the listener knows
      // which wizard to wake up.
      waitingForCallback: false,
      fallbackTimer: null,
    };
    try {
      const resp = await API.callJsonApi("channels_wizard", { channel_type: channelType });
      if (!resp.success) {
        this.wizard.error = resp.error || "Failed to load wizard";
        return;
      }
      this.wizard.steps = resp.steps || [];
      // Pre-fill default values into the inputs map.
      for (const step of this.wizard.steps) {
        for (const field of step.fields || []) {
          if (field.default !== null && field.default !== undefined) {
            this.wizard.inputs[field.id] = field.default;
          }
        }
      }
      // Spec 09 task 1.13: suggest a unique bot_name based on what
      // already exists for this platform. The provisioner's field
      // default is just "default"; we override it here when that
      // name is already taken so the operator doesn't have to retype.
      this.wizard.inputs.bot_name = this._suggestBotName(channelType);
      this._setCurrentStep(0);
    } catch (e) {
      this.wizard.error = String(e);
    }
  },

  /**
   * Pick the first name in ["default", "bot1", "bot2", ...] not yet
   * taken by an existing bot on this platform. Hits placeholder rows
   * too (bot_name === "") so adding the first bot still suggests
   * "default".
   */
  _suggestBotName(channelType) {
    const taken = new Set(
      this.channels
        .filter((c) => c.channel_type === channelType && c.bot_name)
        .map((c) => c.bot_name)
    );
    if (!taken.has("default")) return "default";
    for (let i = 1; i < 100; i++) {
      const candidate = `bot${i}`;
      if (!taken.has(candidate)) return candidate;
    }
    return "default";
  },

  cancelWizard() {
    if (this.wizard?.fallbackTimer) {
      clearTimeout(this.wizard.fallbackTimer);
    }
    this.wizard = null;
  },

  _setCurrentStep(idx) {
    if (!this.wizard) return;
    this.wizard.currentStepIdx = idx;
    this.wizard.currentStep = this.wizard.steps[idx] || null;
    this.wizard.error = null;
    this._maybeStartOauthCountdown();
  },

  _findStepIdx(stepId) {
    if (!this.wizard) return -1;
    return this.wizard.steps.findIndex((s) => s.id === stepId);
  },

  /**
   * Advance the wizard by submitting the current step's inputs.
   */
  async submitStep() {
    if (!this.wizard?.currentStep) return;
    this.wizard.busy = true;
    this.wizard.error = null;
    try {
      const inputs = this._currentInputs();
      const resp = await API.callJsonApi("channels_provision", {
        channel_type: this.wizard.channelType,
        step_id: this.wizard.currentStep.id,
        inputs,
        session_id: this.wizard.sessionId,
      });
      if (!resp.success) {
        this.wizard.error =
          resp.result?.error || resp.error || "Provisioning failed";
        return;
      }
      this._handleStepResult(resp);
    } catch (e) {
      this.wizard.error = String(e);
    } finally {
      this.wizard.busy = false;
    }
  },

  _currentInputs() {
    // Slice the global `inputs` dict down to just the current step's
    // declared fields. Belt-and-braces — keeps the server from seeing
    // stale fields from earlier steps.
    if (!this.wizard?.currentStep) return {};
    const out = {};
    for (const field of this.wizard.currentStep.fields || []) {
      if (this.wizard.inputs[field.id] !== undefined) {
        out[field.id] = this.wizard.inputs[field.id];
      }
    }
    return out;
  },

  _handleStepResult(resp) {
    if (!this.wizard) return;
    this.wizard.sessionId = resp.session_id || this.wizard.sessionId;
    const result = resp.result || {};
    this.wizard.result = result;

    if (result.terminal) {
      this._setCurrentStep(this.wizard.currentStepIdx);
      return;
    }

    // Advance to the next step first, THEN stash the URL override on it.
    // Provisioners emit ``url_override`` describing where the user should
    // go next, which corresponds to the next step (link_with_callback /
    // link_with_paste), not the just-submitted input step.
    if (result.next_step) {
      const idx = this._findStepIdx(result.next_step);
      if (idx >= 0) {
        this._setCurrentStep(idx);
        if (result.url_override && this.wizard.currentStep) {
          this.wizard.currentStep.url = result.url_override;
        }
        return;
      }
    }

    // No explicit advance — re-render the current step (e.g. install
    // step that just emits the URL).
    if (result.url_override && this.wizard.currentStep) {
      this.wizard.currentStep.url = result.url_override;
    }
    this._setCurrentStep(this.wizard.currentStepIdx);
  },

  /**
   * Open the install URL for a `link_with_callback` / `link_with_paste`
   * step. Tracks that we're waiting so the OAuth listener can wake
   * us up when Slack redirects back.
   */
  openInstallUrl() {
    if (!this.wizard?.currentStep?.url) return;
    this.wizard.waitingForCallback =
      this.wizard.currentStep.kind === "link_with_callback";
    window.open(
      this.wizard.currentStep.url,
      "_blank",
      "noopener,noreferrer"
    );
  },

  _maybeStartOauthCountdown() {
    if (!this.wizard) return;
    if (this.wizard.fallbackTimer) {
      clearTimeout(this.wizard.fallbackTimer);
      this.wizard.fallbackTimer = null;
    }
    if (this.wizard.currentStep?.kind !== "link_with_callback") return;

    const ms =
      (this.wizard.currentStep.timeout_s || OAUTH_TIMEOUT_FALLBACK_MS_DEFAULT / 1000) * 1000;
    this.wizard.fallbackTimer = setTimeout(() => {
      if (!this.wizard) return;
      if (this.wizard.waitingForCallback) {
        // OAuth didn't come back in time — flip to the paste-fallback
        // step if the wizard declared one, otherwise just surface a
        // hint message.
        this.wizard.waitingForCallback = false;
        const fallback = this.wizard.currentStep?.next_on_timeout;
        if (fallback) {
          const idx = this._findStepIdx(fallback);
          if (idx >= 0) this._setCurrentStep(idx);
        }
      }
    }, ms);
  },

  // ------------------------------------------------------------------
  // OAuth callback listener (spec 08 D3 auto-capture)
  // ------------------------------------------------------------------

  _installOauthListener() {
    if (this._oauthListener) return;
    this._oauthListener = (event) => {
      const data = event?.data;
      if (!data || data.type !== "hyperagent0:channel-oauth-callback") return;
      // The callback page minted the session id from the URL query;
      // accept it when it matches our active wizard.
      if (!this.wizard) return;
      if (data.session_id !== this.wizard.sessionId) return;
      this.wizard.waitingForCallback = false;
      if (this.wizard.fallbackTimer) {
        clearTimeout(this.wizard.fallbackTimer);
        this.wizard.fallbackTimer = null;
      }
      const result = data.result || {};
      if (result.next_step) {
        const idx = this._findStepIdx(result.next_step);
        if (idx >= 0) {
          this._setCurrentStep(idx);
          return;
        }
      }
      if (result.error) {
        this.wizard.error = result.error;
      }
    };
    window.addEventListener("message", this._oauthListener);
  },

  _removeOauthListener() {
    if (this._oauthListener) {
      window.removeEventListener("message", this._oauthListener);
      this._oauthListener = null;
    }
  },

  // ------------------------------------------------------------------
  // Apply / test / bind
  // ------------------------------------------------------------------

  async applyAll() {
    this.loading = true;
    try {
      await API.callJsonApi("channels_apply", {});
      await this._statusCheck();
    } catch (e) {
      console.error("channels_apply failed:", e);
    } finally {
      this.loading = false;
    }
  },

  async testConnection(channelType, botName = "") {
    // Composite key matches the cardKey() shape so the result lands
    // on the right card. The /channels_test endpoint itself is still
    // channel-level (no bot_name dispatch), so multi-bot installs
    // hide the Test button on bots #2+ — see isFirstBotOfPlatform.
    const key = `${channelType}:${botName || ""}`;
    this.testResults[key] = { busy: true };
    try {
      const resp = await API.callJsonApi("channels_test", {
        channel_type: channelType,
      });
      this.testResults[key] = {
        busy: false,
        ok: !!resp.success,
        message: resp.message || resp.error || "",
      };
    } catch (e) {
      this.testResults[key] = {
        busy: false,
        ok: false,
        message: String(e),
      };
    }
  },

  async bindProject(channelType, chatId, projectName) {
    try {
      const resp = await API.callJsonApi("channels_bind_project", {
        channel: channelType,
        chat_id: chatId || null,
        project: projectName || null,
      });
      if (resp.success) {
        // Patch the local mirror immediately; the next poll
        // refreshes from the server.
        const ch = this.channels.find((c) => c.channel_type === channelType);
        if (ch) ch.project_binding = resp.project_binding || {};
      }
      return resp.success;
    } catch (e) {
      console.error("channels_bind_project failed:", e);
      return false;
    }
  },
};

const store = createStore("channelsStore", model);
export { store };
