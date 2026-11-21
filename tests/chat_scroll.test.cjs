// Offline scroll lifecycle tests; no browser, network or third-party packages.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(
    path.join(__dirname, "../src/ai_consultant_bot/chat_scroll.js"), "utf8"
);

function fixture({nested = false} = {}) {
    const image = new EventTarget();
    const scroller = Object.assign(new EventTarget(), {
        dataset: {}, scrollTop: 0, scrollHeight: 1600, isConnected: true, children: [{}]
    });
    const content = nested ? {parentElement: scroller} : scroller;
    content.querySelectorAll = () => [image];
    const frames = new Map();
    const timers = new Map();
    const observers = [];
    let serial = 0;
    const context = {
        document: {querySelector: () => content},
        getComputedStyle: element => ({overflowY: element === scroller ? "auto" : "visible"}),
        requestAnimationFrame: callback => {frames.set(++serial, callback); return serial;},
        cancelAnimationFrame: id => frames.delete(id),
        setTimeout: callback => {timers.set(++serial, callback); return serial;},
        clearTimeout: id => timers.delete(id),
        ResizeObserver: class {
            constructor(callback) {this.callback = callback; observers.push(this);}
            observe() {}
            disconnect() {this.disconnected = true;}
        }
    };
    return {
        image, scroller, observers,
        render(view = "1:0:1") {vm.runInNewContext(script.replace("__CHAT_VIEW__", view), context);},
        frame() {
            const pending = [...frames.values()]; frames.clear();
            for (const callback of pending) callback();
        },
        expire() {for (const callback of [...timers.values()]) callback();},
    };
}

test("opens at the latest message and follows a delayed photo before user input", () => {
    const f = fixture({nested: true});
    f.render(); f.frame();
    assert.equal(f.scroller.scrollTop, 1600);
    f.scroller.scrollHeight = 2000;
    f.image.dispatchEvent(new Event("load")); f.frame();
    assert.equal(f.scroller.scrollTop, 2000);
});

for (const event of ["wheel", "touchstart", "pointerdown", "keydown"]) {
    test(`${event} stops photo/resize scrolling, including after timer refreshes`, () => {
        const f = fixture();
        f.render(); f.frame();
        f.scroller.dispatchEvent(new Event(event));
        f.scroller.scrollTop = 200;
        f.image.dispatchEvent(new Event("load"));
        f.observers[0].callback();
        f.render(); f.frame();
        assert.equal(f.scroller.scrollTop, 200);
        assert.equal(f.observers.length, 1);
        assert.ok(f.observers[0].disconnected);
    });
}

test("switching chat/page or refreshing the full page initializes the bottom again", () => {
    const f = fixture();
    for (const view of ["1:0:1", "2:0:1", "2:50:1", "2:50:2"]) {
        f.scroller.scrollTop = 50;
        f.render(view); f.frame();
        assert.equal(f.scroller.scrollTop, 1600);
    }
    assert.ok(f.observers.slice(0, -1).every(observer => observer.disconnected));
});

test("initialization expires even if a remote photo does not load", () => {
    const f = fixture();
    f.render(); f.frame(); f.expire();
    f.scroller.scrollTop = 100;
    f.image.dispatchEvent(new Event("load")); f.frame();
    assert.equal(f.scroller.scrollTop, 100);
    assert.ok(f.observers[0].disconnected);
});

test("user input cancels even the first queued scroll frame", () => {
    const f = fixture();
    f.render();
    f.scroller.dispatchEvent(new Event("wheel"));
    f.scroller.scrollTop = 75;
    f.frame();
    assert.equal(f.scroller.scrollTop, 75);
});
