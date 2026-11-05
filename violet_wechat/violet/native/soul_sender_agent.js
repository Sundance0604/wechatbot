// Persistent sender appended after the validated text-object factory.
// Output layout was verified at Weixin.dll+0x17dcca0: {int,int,string,shared_ptr}.
const nativeDelete = new NativeFunction(at(spec.functions.operator_delete), 'void', ['pointer','uint64']);
const dispatchText = new NativeFunction(at(spec.functions.dispatch_candidate), 'pointer',
  ['pointer','pointer','pointer','int']);
const releaseCode = Memory.alloc(Process.pageSize);
const releaseHex = '534883ec204889cb4885db741df0ff4b087517488b034889d9ff10f0ff4b0c7509488b034889d9ff50084883c4205bc3';
releaseCode.writeByteArray(releaseHex.match(/../g).map(x=>parseInt(x,16)));
Memory.protect(releaseCode, Process.pageSize, 'r-x');
// Matches the native shared_ptr teardown: atomic strong--, Destroy, atomic weak--, Delete_this.
const releaseShared = new NativeFunction(releaseCode,'void',['pointer']);
let pending = null;
let busy = false;
let closed = false;

function inspectManager() {
  const manager = ptr(spec.manager_address);
  requireValue(manager.readPointer().equals(at('0x93b68e8')), 'Sender manager changed');
  const state = manager.add(0xbf8).readPointer();
  requireValue(!state.isNull(), 'Sender state missing');
  const mask = state.add(0xc0).readU64().toNumber();
  requireValue(mask >= 1 && mask <= 1048575 && (mask & (mask+1)) === 0, 'Sender state layout mismatch');
  return {pid:Process.id, worker_thread:spec.ui_thread_id, manager:manager.toString()};
}
function releaseString(address) {
  const capacity = address.add(24).readU64().toNumber();
  requireValue(capacity <= 1024*1024, 'Unexpected output string capacity');
  if (capacity < 16) return;
  let storage = address.readPointer(), size = capacity + 1;
  if (size >= 4096) {
    const base = storage.sub(8).readPointer();
    const adjustment = storage.sub(8).sub(base).toUInt32();
    requireValue(adjustment <= 31, 'Output string allocation header mismatch');
    storage = base; size = capacity + 0x28;
  }
  nativeDelete(storage, size);
}
function cleanupOutput(output) {
  const control = output.add(0x30).readPointer();
  if (!control.isNull()) {
    const vtable = control.readPointer();
    requireValue(vtable.compare(wx.base) >= 0 && vtable.compare(wx.base.add(wx.size)) < 0,
      'Unexpected output shared_ptr vtable');
    releaseShared(control);
  }
  releaseString(output.add(8));
  output.writeByteArray(new Uint8Array(56));
}
function selfTestRelease() {
  let destroyed = 0, deleted = 0;
  const callbackErrors = [];
  const table = at(spec.control_vtable_rva);
  const originalDestroy = new NativeFunction(table.readPointer(),'void',['pointer']);
  const originalDelete = new NativeFunction(table.add(8).readPointer(),'void',['pointer']);
  // Frida suppresses nested Interceptor callbacks on its native-call path. Explicit callbacks
  // on our own, unshared test blocks let us observe the actual teardown without global hooks.
  const wrappedTable = Memory.alloc(64);
  wrappedTable.writeByteArray(table.readByteArray(64));
  const destroyCallback = new NativeCallback(control => {
    try { originalDestroy(control); destroyed++; } catch (error) { callbackErrors.push(String(error)); }
  },'void',['pointer']);
  const deleteCallback = new NativeCallback(control => {
    // Delete_this may dispatch through additional native virtual destructor slots.
    control.writePointer(table);
    try { originalDelete(control); deleted++; } catch (error) { callbackErrors.push(String(error)); }
  },'void',['pointer']);
  wrappedTable.writePointer(destroyCallback);
  wrappedTable.add(8).writePointer(deleteCallback);
  try {
    for (let i=0;i<3;i++) {
      const {cell,control} = createText();
      // An extra test-only strong reference verifies the atomic decrement before destruction.
      control.add(8).writeU32(2);
      releaseShared(control);
      requireValue(control.add(8).readU32() === 1, 'Atomic decrement failed');
      control.writePointer(wrappedTable);
      releaseShared(control);
      cell.writeByteArray(new Uint8Array(16));
    }
    send({event:'ownership_selftest_counts', destroyed, deleted, callbackErrors});
    requireValue(destroyed === 3 && deleted === 3 && callbackErrors.length === 0,
      'Shared ownership teardown self-test failed: destroyed=' + destroyed + ', deleted=' + deleted);
    return {passed:true, destroyed, deleted, worker_thread:Process.getCurrentThreadId()};
  } finally { pinned.length = 0; }
}
function sendPending(job) {
  inspectManager();
  spec.text = job.text;
  spec.recipient = job.recipient;
  const {cell} = createText();
  const output = Memory.alloc(56);
  pinned.push(output);
  output.writeByteArray(new Uint8Array(56));
  send({event:'send_started', request_id:job.id, time_ms:Date.now()});
  const returned = dispatchText(ptr(spec.manager_address),output,cell,1);
  requireValue(returned.equals(output), 'Sender return pointer mismatch');
  const result = {invoked:true, request_id:job.id, error_code:output.readS32(),
    error_subcode:output.add(4).readS32(), output_cleanup:true};
  cleanupOutput(output);
  // Input ownership was consumed by dispatch. Its cell and UTF-8 scratch storage are Frida-owned.
  cell.writeByteArray(new Uint8Array(16));
  pinned.length = 0;
  return result;
}
const workerHook = Interceptor.attach(at(spec.functions.coroutine_entry), {
  onEnter() {
    if (!pending || busy || Process.getCurrentThreadId() !== spec.ui_thread_id) return;
    const job = pending; pending = null; busy = true; clearTimeout(job.timer);
    try { job.resolve(job.test ? selfTestRelease() : sendPending(job)); }
    catch (error) { job.reject(error); }
    finally { busy = false; }
  }
});
function enqueue(text,id,test=false,recipient=spec.recipient) {
  requireValue(!closed && !pending && !busy, 'Sender unavailable or busy');
  return new Promise((resolve,reject)=> {
    const job = {text,id,test,recipient,resolve,reject};
    job.timer = setTimeout(()=> {
      if (pending !== job) return;
      pending = null; reject(Error('No worker coroutine within 30 seconds; no send was invoked'));
    },30000);
    pending = job;
  });
}
rpc.exports = {
  inspect() { return inspectManager(); },
  selftest() { return enqueue(spec.text,'selftest',true); },
  sendtext(text,id,recipient) {
    requireValue(typeof text === 'string' && text.trim().length > 0 && !text.includes('\x00'), 'Invalid message');
    requireValue(typeof recipient === 'string' && /^[^\s\x00]{1,100}$/.test(recipient), 'Invalid recipient identifier');
    return enqueue(text,id,false,recipient);
  },
  cancelpending() {
    if (pending) {
      const job = pending; pending = null; clearTimeout(job.timer);
      job.reject(Error('SEND_CANCELLED_BEFORE_INVOCATION'));
      return {cancelled:true,busy};
    }
    return {cancelled:false,busy};
  },
  stop() {
    closed = true;
    if (pending) { clearTimeout(pending.timer); pending.reject(Error('Sender stopped before invocation')); pending=null; }
    workerHook.detach();
    return {stopped:true,busy};
  }
};
