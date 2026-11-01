"use strict";
/* The I/O point editor, shared by the edge Commissioning tab and the cloud portal.
   TagForm.open(tag | null, { exists, save, remove }):
     exists(name) -> bool       – for new points, reject duplicate names
     save(def, isNew) -> Promise – throw to show the error in the dialog
     remove(name) -> Promise     – optional; shows a Delete button for existing points */

const TagForm = (() => {
  const MARKUP = `
<dialog id="tag-dialog" class="wide-dialog" aria-labelledby="td-title">
    <form id="tag-form" novalidate>
      <h3 id="td-title">Edit I/O point</h3>
      <div class="form-grid">
        <div class="field"><label for="td-name">Tag name</label><input id="td-name" class="mono" required autocomplete="off" spellcheck="false"></div>
        <div class="field span2"><label for="td-description">Description</label><input id="td-description" autocomplete="off"></div>
        <div class="field"><label for="td-kind">Kind</label>
          <select id="td-kind">
            <option value="DI">DI – digital input</option><option value="DO">DO – digital output</option>
            <option value="AI">AI – analog input</option><option value="AO">AO – analog output</option>
            <option value="SP">SP – setpoint</option>
          </select></div>
        <div class="field"><label for="td-type">Value type</label>
          <select id="td-type"><option value="bool">Digital (on/off)</option><option value="float">Analog (number)</option></select></div>
        <div class="field analog-only"><label for="td-unit">Unit</label><input id="td-unit" autocomplete="off"></div>
        <div class="field analog-only"><label for="td-decimals">Decimals</label><input id="td-decimals" type="number" min="0" max="6"></div>
        <div class="field analog-only"><label for="td-min">Range min</label><input id="td-min" type="number" step="any" inputmode="decimal"></div>
        <div class="field analog-only"><label for="td-max">Range max</label><input id="td-max" type="number" step="any" inputmode="decimal"></div>
        <div class="field"><label for="td-role">Who may change it</label>
          <select id="td-role"><option value="operator">Operator</option><option value="engineer">Engineer</option><option value="integrator">Integrator</option></select></div>
      </div>
      <label class="check"><input type="checkbox" id="td-writable"> Operators can write this point from the HMI</label>

      <div id="td-modbus-section">
      <h4>Modbus address</h4>
      <div class="form-grid">
        <div class="field"><label for="td-table">Register type</label>
          <select id="td-table">
            <option value="">Not mapped</option>
            <option value="coil">Coil (0x)</option><option value="discrete">Discrete input (1x)</option>
            <option value="input">Input register (3x)</option><option value="holding">Holding register (4x)</option>
          </select></div>
        <div class="field"><label for="td-address">Address (0-based)</label><input id="td-address" type="number" min="0" max="65535" inputmode="numeric"></div>
        <div class="field analog-only"><label for="td-mtype">Data type</label>
          <select id="td-mtype">
            <option value="int16">INT16 – signed (1 register)</option><option value="uint16">UINT16 – unsigned (1 register)</option>
            <option value="int32">INT32 / DINT (2 registers)</option><option value="uint32">UINT32 / UDINT (2 registers)</option>
            <option value="float32">FLOAT32 / REAL (2 registers)</option>
          </select></div>
        <div class="field analog-only"><label for="td-word">Word order (32-bit)</label>
          <select id="td-word"><option value="big">High word first (ABCD)</option><option value="little">Low word first (CDAB)</option></select></div>
        <div class="field analog-only"><label for="td-scale">Scale (raw ÷)</label><input id="td-scale" type="number" step="any" min="0" inputmode="decimal"></div>
        <div class="field digital-only"><label for="td-bit">Bit in register (0–15)</label><input id="td-bit" type="number" min="0" max="15" inputmode="numeric" placeholder="whole register"></div>
      </div>
      </div>
      <div id="td-s7-section" hidden>
      <h4>S7 address</h4>
      <div class="form-grid">
        <div class="field"><label for="td-s7addr">Address</label><input id="td-s7addr" class="mono" autocomplete="off" spellcheck="false" placeholder="%DB1.DBD4"></div>
        <div class="field"><label for="td-s7type">Data type</label>
          <select id="td-s7type">
            <option value="">From address</option>
            <option value="int">INT</option><option value="uint">UINT</option><option value="word">WORD</option>
            <option value="real">REAL</option><option value="dint">DINT</option><option value="udint">UDINT</option><option value="dword">DWORD</option>
            <option value="byte">BYTE</option><option value="sint">SINT</option><option value="usint">USINT</option>
          </select></div>
        <div class="field analog-only"><label for="td-s7scale">Scale (raw ÷)</label><input id="td-s7scale" type="number" step="any" min="0" inputmode="decimal"></div>
      </div>
      <p class="hint">TIA notation: %I0.0, %Q0.1, %M10.3, %IW64, %MD20, %DB1.DBX0.0, %DB1.DBW2, %DB1.DBD4. DBs must have “Optimized block access” turned off.</p>
      </div>

      <h4>Alarm</h4>
      <div class="form-grid digital-only">
        <div class="field"><label for="td-alarm-state">Raise alarm when</label>
          <select id="td-alarm-state"><option value="">No alarm</option><option value="true">ON</option><option value="false">OFF</option></select></div>
        <div class="field"><label for="td-alarm-priority">Priority</label>
          <select id="td-alarm-priority"><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></select></div>
        <div class="field span2"><label for="td-alarm-message">Alarm message</label><input id="td-alarm-message" autocomplete="off"></div>
      </div>
      <div class="form-grid analog-only">
        <div class="field"><label for="td-alarm-hihi">High-high</label><input id="td-alarm-hihi" class="mono" autocomplete="off" placeholder="—"></div>
        <div class="field"><label for="td-alarm-hi">High</label><input id="td-alarm-hi" class="mono" autocomplete="off" placeholder="—"></div>
        <div class="field"><label for="td-alarm-lo">Low</label><input id="td-alarm-lo" class="mono" autocomplete="off" placeholder="—"></div>
        <div class="field"><label for="td-alarm-lolo">Low-low</label><input id="td-alarm-lolo" class="mono" autocomplete="off" placeholder="—"></div>
        <div class="field"><label for="td-alarm-deadband">Deadband</label><input id="td-alarm-deadband" type="number" step="any" min="0" inputmode="decimal"></div>
      </div>
      <p class="hint analog-only">A limit can be a number, or the name of a setpoint tag that holds it (e.g. SP_LevelHiAlarm).</p>

      <p class="form-error" id="td-error" role="alert" hidden></p>
      <div class="dialog-actions">
        <button type="button" class="btn danger" id="td-delete">Delete point</button>
        <span class="spacer"></span>
        <button type="button" class="btn" id="td-cancel">Cancel</button>
        <button class="btn primary">Save &amp; apply</button>
      </div>
    </form>
  </dialog>
`;
  let editing = null;
  let protocol = "modbus";
  let handlers = {};

  function ensure() {
    if ($("#tag-dialog")) return;
    document.body.insertAdjacentHTML("beforeend", MARKUP);
    const dlg = $("#tag-dialog");
    $("#td-cancel").onclick = () => dlg.close();
    $("#td-type").onchange = sync;
    $("#td-table").onchange = sync;
    $("#td-mtype").onchange = sync;
    $("#td-kind").onchange = () => {
      // Default the value type from the kind; setpoints can be either.
      const k = $("#td-kind").value;
      if (k === "DI" || k === "DO") $("#td-type").value = "bool";
      if (k === "AI" || k === "AO") $("#td-type").value = "float";
      sync();
    };
    $("#td-delete").onclick = remove;
    $("#tag-form").addEventListener("submit", (e) => { e.preventDefault(); save(); });
  }

  function sync() {
    const analog = $("#td-type").value === "float";
    for (const f of $$("#tag-dialog .analog-only")) f.hidden = !analog;
    for (const f of $$("#tag-dialog .digital-only")) f.hidden = analog;
    // Data type / word order / bit only apply to registers; word order only to 32-bit types.
    const registers = ["input", "holding"].includes($("#td-table").value);
    const wide = ["int32", "uint32", "float32"].includes($("#td-mtype").value);
    const field = (id) => $(id).closest(".field");
    if (!registers) for (const id of ["#td-mtype", "#td-word", "#td-bit"]) field(id).hidden = true;
    if (!wide) field("#td-word").hidden = true;
  }

  function open(tag, opts) {
    ensure();
    editing = tag;
    handlers = opts || {};
    const t = tag || { name: "", kind: "AI", type: "float", decimals: 1, role: "operator" };
    const set = (id, v) => { $(id).value = v ?? ""; };
    $("#td-title").textContent = tag ? `Edit ${tag.name}` : "Add I/O point";
    set("#td-name", t.name);
    $("#td-name").readOnly = !!tag;
    set("#td-description", t.description);
    set("#td-kind", t.kind);
    set("#td-type", t.type);
    set("#td-unit", t.unit);
    set("#td-decimals", t.decimals ?? 1);
    set("#td-min", t.min);
    set("#td-max", t.max);
    set("#td-role", t.role || "operator");
    $("#td-writable").checked = !!t.writable;
    set("#td-table", t.modbus?.table);
    set("#td-address", t.modbus?.address);
    set("#td-scale", t.modbus?.scale ?? 1);
    set("#td-mtype", t.modbus?.type || "int16");
    set("#td-word", t.modbus?.word_order || "big");
    set("#td-bit", t.modbus?.bit);
    set("#td-s7addr", t.s7?.address);
    set("#td-s7type", t.s7?.type || "");
    set("#td-s7scale", t.s7?.scale ?? 1);
    protocol = (opts && opts.protocol) || (t.s7 && !t.modbus ? "s7" : "modbus");
    $("#td-modbus-section").hidden = protocol === "s7";
    $("#td-s7-section").hidden = protocol !== "s7";
    const al = t.alarm || {};
    set("#td-alarm-state", t.type === "bool" && "state" in al ? String(al.state) : "");
    set("#td-alarm-priority", al.priority || "medium");
    set("#td-alarm-message", al.message);
    for (const k of ["hihi", "hi", "lo", "lolo"]) set(`#td-alarm-${k}`, t.type === "float" ? al[k] : "");
    set("#td-alarm-deadband", al.deadband);
    $("#td-delete").hidden = !tag || !handlers.remove;
    $("#td-error").hidden = true;
    sync();
    $("#tag-dialog").showModal();
    (tag ? $("#td-description") : $("#td-name")).focus();
  }

  function read() {
    const num = (id) => ($(id).value.trim() === "" ? undefined : Number($(id).value));
    const type = $("#td-type").value;
    const def = { ...(editing || {}) };
    Object.assign(def, {
      name: $("#td-name").value.trim(),
      description: $("#td-description").value.trim(),
      kind: $("#td-kind").value,
      type,
      role: $("#td-role").value,
      writable: $("#td-writable").checked,
    });
    for (const key of ["unit", "min", "max", "decimals", "modbus", "s7", "alarm"]) delete def[key];
    if (type === "float") {
      if ($("#td-unit").value.trim()) def.unit = $("#td-unit").value.trim();
      def.min = num("#td-min");
      def.max = num("#td-max");
      def.decimals = num("#td-decimals") ?? 1;
    }
    // Only the asset's protocol is edited; a mapping for the other protocol is kept as it was.
    if (protocol === "s7") {
      if (editing?.modbus) def.modbus = editing.modbus;
      const address = $("#td-s7addr").value.trim();
      if (address) {
        def.s7 = { address };
        if ($("#td-s7type").value) def.s7.type = $("#td-s7type").value;
        if (type === "float") def.s7.scale = num("#td-s7scale") ?? 1;
      }
    } else {
      if (editing?.s7) def.s7 = editing.s7;
      const table = $("#td-table").value;
      if (table) {
        def.modbus = { table, address: num("#td-address") };
        const registers = table === "input" || table === "holding";
        if (type === "float") {
          def.modbus.scale = num("#td-scale") ?? 1;
          if (registers && $("#td-mtype").value !== "int16") def.modbus.type = $("#td-mtype").value;
          if (registers && def.modbus.type && $("#td-word").value === "little") def.modbus.word_order = "little";
        } else if (registers && num("#td-bit") !== undefined) {
          def.modbus.bit = num("#td-bit");
        }
      }
    }
    if (type === "bool") {
      const state = $("#td-alarm-state").value;
      if (state) {
        def.alarm = { state: state === "true", priority: $("#td-alarm-priority").value,
          message: $("#td-alarm-message").value.trim() || `${def.description || def.name} alarm` };
      }
    } else {
      const alarm = {};
      for (const k of ["hihi", "hi", "lo", "lolo"]) {
        const raw = $(`#td-alarm-${k}`).value.trim();
        if (raw) alarm[k] = Number.isFinite(Number(raw)) ? Number(raw) : raw;
      }
      if (Object.keys(alarm).length) {
        const db = num("#td-alarm-deadband");
        if (db !== undefined) alarm.deadband = db;
        def.alarm = alarm;
      }
    }
    for (const [k, v] of Object.entries(def)) if (v === undefined) delete def[k];
    return def;
  }

  async function save() {
    const def = read();
    const err = $("#td-error");
    const fail = (msg) => { err.textContent = msg; err.hidden = false; };
    if (!def.name) return fail("Give the point a tag name.");
    if (protocol !== "s7" && def.modbus && !Number.isInteger(def.modbus.address)) {
      return fail("Enter the Modbus address, or set the register type to “Not mapped”.");
    }
    if (!editing && handlers.exists?.(def.name)) return fail(`A point called ${def.name} already exists.`);
    try {
      await handlers.save(def, !editing);
    } catch (ex) {
      return fail(ex.message);
    }
    $("#tag-dialog").close();
  }

  async function remove() {
    const name = editing.name;
    $("#tag-dialog").close();
    const ok = await confirmChange(`Delete ${name}?`,
      "The point is removed from this asset's I/O, screens and trends.", "Delete");
    if (!ok) { $("#tag-dialog").showModal(); return; }
    try { await handlers.remove(name); } catch (e) { toast(e.message, "error"); }
  }

  // Short address text for tables, e.g. "4x 10 FLOAT32 ÷10" or "3x 12.3".
  function address(t, proto) {
    if ((proto === "s7" || !t.modbus) && t.s7) {
      const sc = t.s7.scale && t.s7.scale !== 1 ? ` ÷${t.s7.scale}` : "";
      return `${t.s7.address.toUpperCase().replace(/^(?!%)/, "%")}${t.s7.type ? ` ${t.s7.type.toUpperCase()}` : ""}${sc}`;
    }
    const m = t.modbus;
    if (!m || proto === "s7") return "—";
    const prefix = { coil: "0x", discrete: "1x", input: "3x", holding: "4x" }[m.table] || m.table;
    if (m.bit !== undefined) return `${prefix} ${m.address}.${m.bit}`;
    const dtype = m.type && m.type !== "int16" ? ` ${m.type.toUpperCase()}${m.word_order === "little" ? " (CDAB)" : ""}` : "";
    return `${prefix} ${m.address}${dtype}${m.scale && m.scale !== 1 ? ` ÷${m.scale}` : ""}`;
  }

  return { open, address };
})();
