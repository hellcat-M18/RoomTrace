const DB_NAME = "roomtrace-web-captures";
const DB_VERSION = 1;

function openDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains("meta")) {
        database.createObjectStore("meta", { keyPath: "captureId" });
      }
      if (!database.objectStoreNames.contains("frames")) {
        const store = database.createObjectStore("frames", { keyPath: ["captureId", "frameId"] });
        store.createIndex("captureId", "captureId", { unique: false });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("IndexedDB could not be opened"));
  });
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("IndexedDB request failed"));
  });
}

export class CaptureStore {
  constructor(captureId) {
    this.captureId = captureId;
    this.database = openDatabase();
    this.writeChain = Promise.resolve();
  }

  async begin(meta) {
    const database = await this.database;
    await new Promise((resolve, reject) => {
      const transaction = database.transaction(["meta"], "readwrite");
      transaction.objectStore("meta").put(meta);
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error || new Error("capture metadata could not be saved"));
    });
  }

  queueFrame(record, imageBlob, depthBlob, confidenceBlob) {
    this.writeChain = this.writeChain.then(async () => {
      const database = await this.database;
      await new Promise((resolve, reject) => {
        const transaction = database.transaction(["frames"], "readwrite");
        transaction.objectStore("frames").put({
          captureId: this.captureId,
          frameId: record.frame_id,
          record,
          imageBlob,
          depthBlob,
          confidenceBlob,
        });
        transaction.oncomplete = resolve;
        transaction.onerror = () => reject(transaction.error || new Error("capture frame could not be saved"));
      });
    });
    return this.writeChain;
  }

  async flush() {
    await this.writeChain;
  }

  async getFrames() {
    await this.flush();
    const database = await this.database;
    const transaction = database.transaction(["frames"], "readonly");
    const index = transaction.objectStore("frames").index("captureId");
    const result = await requestResult(index.getAll(this.captureId));
    return result.sort((left, right) => left.frameId - right.frameId);
  }

  async discard() {
    await this.flush();
    const database = await this.database;
    await new Promise((resolve, reject) => {
      const transaction = database.transaction(["meta", "frames"], "readwrite");
      transaction.objectStore("meta").delete(this.captureId);
      const frameIndex = transaction.objectStore("frames").index("captureId");
      const cursorRequest = frameIndex.openCursor(IDBKeyRange.only(this.captureId));
      cursorRequest.onsuccess = () => {
        const cursor = cursorRequest.result;
        if (!cursor) return;
        cursor.delete();
        cursor.continue();
      };
      cursorRequest.onerror = () => reject(cursorRequest.error || new Error("capture frames could not be deleted"));
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error || new Error("capture data could not be deleted"));
    });
  }

  async close() {
    const database = await this.database;
    database.close();
  }
}
