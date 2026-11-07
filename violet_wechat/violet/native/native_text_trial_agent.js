// Construction only: no NativeFunction is created for the dispatch candidate.
const spec = __PROFILE__;
const wx = Process.getModuleByName('Weixin.dll');
const at = rva => wx.base.add(rva);
const requireValue = (condition, message) => { if (!condition) throw Error(message); };
requireValue(wx.path.replace(/\//g,'\\').toLowerCase() === spec.module_path.replace(/\//g,'\\').toLowerCase(), 'Loaded module path mismatch');
for (const [name, rva] of Object.entries(spec.functions)) {
  const prefix = Array.from(new Uint8Array(at(rva).readByteArray(8)))
    .map(x => x.toString(16).padStart(2, '0')).join('');
  requireValue(prefix === spec.prefixes[name], 'Code prefix mismatch: ' + name);
}
requireValue(Process.id === spec.pid, 'PID mismatch');
const make = new NativeFunction(at(spec.functions.make_text_shared), 'pointer', ['pointer']);
const allocate = new NativeFunction(at(spec.functions.operator_new), 'pointer', ['uint64']);
let used = false;
let hooks = [];
function detachHooks() { for (const hook of hooks) hook.detach(); hooks = []; }
const pinned = [];

function readString(address) {
  const length = address.add(16).readU64().toNumber();
  const capacity = address.add(24).readU64().toNumber();
  requireValue(length <= capacity && capacity < 4096, 'Invalid string layout');
  return {length, capacity, text: (capacity > 15 ? address.readPointer() : address).readUtf8String(length)};
}
function fillEmptyString(address, value) {
  requireValue(readString(address).length === 0, 'Expected an empty native string');
  const encoded = Memory.allocUtf8String(value);
  pinned.push(encoded);
  const length = Array.from(value).reduce((n, c) => n +
    (c.codePointAt(0) <= 0x7f ? 1 : c.codePointAt(0) <= 0x7ff ? 2 : c.codePointAt(0) <= 0xffff ? 3 : 4), 0);
  requireValue(length <= 2048, 'Text exceeds bounded trial allocation');
  const bytes = new Uint8Array(encoded.readByteArray(length + 1));
  const capacity = length <= 15 ? 15 : (length | 15);
  const storage = length <= 15 ? address : allocate(capacity + 1);
  requireValue(!storage.isNull(), 'Native allocation failed');
  storage.writeByteArray(bytes.slice(0, length + 1));
  if (length > 15) address.writePointer(storage);
  address.add(16).writeU64(length);
  address.add(24).writeU64(capacity);
  return readString(address);
}
function createText(nativeCell = false) {
  const cell = nativeCell ? allocate(16) : Memory.alloc(16);
  cell.writeByteArray(new Uint8Array(16));
  pinned.push(cell);
  requireValue(make(cell).equals(cell), 'Factory return mismatch');
  const object = cell.readPointer(), control = cell.add(8).readPointer();
  requireValue(object.equals(control.add(16)), 'Control block layout mismatch');
  requireValue(object.readPointer().equals(at(spec.text_vtable_rva)), 'Text class mismatch');
  requireValue(control.readPointer().equals(at(spec.control_vtable_rva)), 'Control class mismatch');
  requireValue(control.add(8).readU32() === 1 && control.add(12).readU32() === 2, 'Unexpected ownership counts');
  const result = {thread_id: Process.getCurrentThreadId(), factory_return_verified: true,
    strong_count: 1, weak_count: 2, uuid_length: readString(object.add(spec.uuid_offset)).length};
  result.recipient = fillEmptyString(object.add(spec.recipient_offset), spec.recipient);
  result.body = fillEmptyString(object.add(spec.body_offset), spec.text);
  object.add(spec.type_offset).writeU32(1);
  object.add(spec.subtype_offset).writeU32(0);
  // Native plain-text factory copies QString::size(), measured in UTF-16 units.
  object.add(spec.body_length_offset).writeU64(spec.text.length);
  result.content_utf16_length = spec.text.length;
  return {cell, object, control, result};
}
