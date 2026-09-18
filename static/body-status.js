(() => {
  "use strict";

  // 兼容事件触发型车身信号：短时间没有状态变化不等于域控离线。
  const DEFAULT_STALE_AFTER_MS = 30000;

  function firstDefined(...values) {
    return values.find((value) => value !== undefined && value !== null);
  }

  function normalizeDoor(value) {
    if (typeof value === "boolean") return value ? "OPEN" : "CLOSED";
    if (typeof value === "number") return value === 1 ? "OPEN" : value === 0 ? "CLOSED" : null;
    const normalized = String(value ?? "").trim().toLowerCase();
    if (["1", "true", "on", "open", "opened", "active"].includes(normalized)) return "OPEN";
    if (["0", "false", "off", "close", "closed", "inactive"].includes(normalized)) return "CLOSED";
    return null;
  }

  function normalizeLock(value) {
    if (typeof value === "boolean") return value ? "LOCKED" : "UNLOCKED";
    if (typeof value === "number") return value === 1 ? "LOCKED" : value === 0 ? "UNLOCKED" : null;
    const normalized = String(value ?? "").trim().toLowerCase();
    if (["1", "true", "locked", "lock"].includes(normalized)) return "LOCKED";
    if (["0", "false", "unlocked", "unlock"].includes(normalized)) return "UNLOCKED";
    return null;
  }

  function normalizeBoolean(value, positiveValues, negativeValues) {
    if (typeof value === "boolean") return value;
    if (typeof value === "number") return value === 1 ? true : value === 0 ? false : null;
    const normalized = String(value ?? "").trim().toLowerCase();
    if (positiveValues.includes(normalized)) return true;
    if (negativeValues.includes(normalized)) return false;
    return null;
  }

  function normalizeConnection(value) {
    return normalizeBoolean(
      value,
      ["1", "true", "connected", "online", "active"],
      ["0", "false", "disconnected", "offline", "inactive"],
    );
  }

  function normalizeCanConnection(value) {
    return normalizeBoolean(
      value,
      ["1", "true", "connected", "normal", "ok", "online", "active"],
      ["0", "false", "disconnected", "error", "abnormal", "fault", "offline", "bus-off"],
    );
  }

  function normalizeLight(value) {
    return normalizeBoolean(
      value,
      ["1", "true", "on", "active"],
      ["0", "false", "off", "inactive"],
    );
  }

  function normalizeTimestamp(value) {
    const timestamp = Number(value);
    return Number.isFinite(timestamp) && timestamp > 0 ? timestamp : null;
  }

  class BodyStatus {
    constructor(values = {}) {
      this.connected = values.connected === true;
      this.canConnected = this.connected && values.canConnected === true;
      this.doors = Object.freeze({
        fl: this.connected ? values.doors?.fl ?? null : null,
        fr: this.connected ? values.doors?.fr ?? null : null,
        rl: this.connected ? values.doors?.rl ?? null : null,
        rr: this.connected ? values.doors?.rr ?? null : null,
      });
      this.centralLock = this.connected ? values.centralLock ?? null : null;
      this.lights = Object.freeze({
        lowBeam: this.connected ? values.lights?.lowBeam ?? null : null,
        highBeam: this.connected ? values.lights?.highBeam ?? null : null,
        turnLeft: this.connected ? values.lights?.turnLeft ?? null : null,
        turnRight: this.connected ? values.lights?.turnRight ?? null : null,
        hazard: this.connected ? values.lights?.hazard ?? null : null,
      });
      this.timestamp = this.connected ? normalizeTimestamp(values.timestamp) : null;
      Object.freeze(this);
    }

    static empty() {
      return new BodyStatus();
    }

    static fromPayload(payload = {}, options = {}) {
      const raw = payload && typeof payload === "object" ? payload : {};
      const controller = raw.controller && typeof raw.controller === "object" ? raw.controller : {};
      const doors = raw.doors && typeof raw.doors === "object" ? raw.doors : {};
      const lights = raw.lights && typeof raw.lights === "object" ? raw.lights : {};
      const now = Number(options.nowMs ?? Date.now());
      const staleAfterMs = Number(options.staleAfterMs ?? DEFAULT_STALE_AFTER_MS);
      const timestamp = normalizeTimestamp(firstDefined(
        raw.timestamp,
        raw.updatedAtMs,
        raw.updated_at_ms,
        raw.captured_at_ms,
        controller.updatedAtMs,
        controller.updated_at_ms,
      ));
      const ageMs = timestamp == null ? Infinity : Math.max(0, now - timestamp);
      const connectionFlag = normalizeConnection(firstDefined(
        raw.connected,
        raw.controllerOnline,
        controller.connected,
        controller.online,
        raw.status,
      ));
      const connected = connectionFlag === true && ageMs <= staleAfterMs;

      if (!connected) return BodyStatus.empty();

      return new BodyStatus({
        connected: true,
        canConnected: normalizeCanConnection(firstDefined(
          raw.canConnected,
          raw.canState,
          raw.can_status,
          controller.canConnected,
          controller.canState,
          controller.can_status,
        )) === true,
        doors: {
          fl: normalizeDoor(firstDefined(raw.Door_FL, doors.fl, doors.frontLeft, doors.front_left)),
          fr: normalizeDoor(firstDefined(raw.Door_FR, doors.fr, doors.frontRight, doors.front_right)),
          rl: normalizeDoor(firstDefined(raw.Door_RL, doors.rl, doors.rearLeft, doors.rear_left)),
          rr: normalizeDoor(firstDefined(raw.Door_RR, doors.rr, doors.rearRight, doors.rear_right)),
        },
        centralLock: normalizeLock(firstDefined(
          raw.centralLock,
          raw.centralLockState,
          raw.CentralLockState,
          raw.central_lock_state,
          raw.lock?.central,
        )),
        lights: {
          lowBeam: normalizeLight(firstDefined(raw.LowBeam, lights.lowBeam, lights.low_beam)),
          highBeam: normalizeLight(firstDefined(raw.HighBeam, lights.highBeam, lights.high_beam)),
          turnLeft: normalizeLight(firstDefined(raw.TurnLeft, lights.turnLeft, lights.turn_left)),
          turnRight: normalizeLight(firstDefined(raw.TurnRight, lights.turnRight, lights.turn_right)),
          hazard: normalizeLight(firstDefined(raw.Hazard, lights.hazard)),
        },
        timestamp,
      });
    }
  }

  class BodyStatusDataSource {
    constructor(options = {}) {
      this.staleAfterMs = Number(options.staleAfterMs || DEFAULT_STALE_AFTER_MS);
      this.current = BodyStatus.empty();
      this.listeners = new Set();
    }

    ingest(payload, nowMs = Date.now()) {
      this.current = BodyStatus.fromPayload(payload, {
        nowMs,
        staleAfterMs: this.staleAfterMs,
      });
      this.listeners.forEach((listener) => listener(this.current));
      return this.current;
    }

    reset() {
      this.current = BodyStatus.empty();
      this.listeners.forEach((listener) => listener(this.current));
    }

    getSnapshot() {
      return this.current;
    }

    subscribe(listener, emitCurrent = true) {
      if (typeof listener !== "function") throw new TypeError("BodyStatus listener must be a function");
      this.listeners.add(listener);
      if (emitCurrent) listener(this.current);
      return () => this.listeners.delete(listener);
    }
  }

  window.VShieldBody = Object.freeze({
    BodyStatus,
    BodyStatusDataSource,
    DEFAULT_STALE_AFTER_MS,
  });
})();
