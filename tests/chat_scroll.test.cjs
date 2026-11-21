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
    const makeElements = () => {
        const scroller = Object.assign(new EventTarget(), {
            scrollHeight: 1600, clientHeight: 400, isConnected: true, children: [{}]
        });
        let scrollTop = 0;
        Object.defineProperty(scroller, "scrollTop", {
            get: () => scrollTop,
            set: value => {
                scrollTop = Math.max(0, Math.min(value, scroller.scrollHeight - scroller.clientHeight));
            },
        });
        const content = nested
            ? Object.assign(new EventTarget(), {parentElement: scroller, isConnected: true})
            : scroller;
        return {scroller, content};
    };
    let current = makeElements();
    let conversationPresent = true;
    const frames = new Map();
    const timers = new Map();
    const observers = [];
    const mutations = [];
    let serial = 0;
    const context = {
        window: {},
        document: {
            body: {},
            querySelector: selector => selector === ".st-key-chat_thread"
                ? current.content.isConnected ? current.content : null
                : conversationPresent ? {} : null,
        },
        getComputedStyle: element => ({overflowY: element === current.scroller ? "auto" : "visible"}),
        requestAnimationFrame: callback => {frames.set(++serial, callback); return serial;},
        cancelAnimationFrame: id => frames.delete(id),
        setTimeout: callback => {timers.set(++serial, callback); return serial;},
        clearTimeout: id => timers.delete(id),
        ResizeObserver: class {
            constructor(callback) {this.callback = callback; observers.push(this);}
            observe() {this.disconnected = false;}
            disconnect() {this.disconnected = true;}
        },
        MutationObserver: class {
            constructor(callback) {this.callback = callback; mutations.push(this);}
            observe() {}
            disconnect() {this.disconnected = true;}
        }
    };
    return {
        get content() {return current.content;},
        get scroller() {return current.scroller;},
        observers, mutations,
        render(view = "1:0:1") {vm.runInNewContext(script.replace("__CHAT_VIEW__", view), context);},
        frame() {
            const pending = [...frames.values()]; frames.clear();
            for (const callback of pending) callback();
        },
        resize() {for (const observer of observers) if (!observer.disconnected) observer.callback();},
        mutate() {for (const observer of mutations) if (!observer.disconnected) observer.callback();},
        expire() {
            const pending = [...timers.values()]; timers.clear();
            for (const callback of pending) callback();
        },
        userScroll(top) {
            current.scroller.dispatchEvent(new Event("wheel"));
            current.scroller.scrollTop = top;
            current.scroller.dispatchEvent(new Event("scroll"));
            this.frame(); this.expire(); this.frame();
        },
        replace() {
            current.content.isConnected = false;
            current.scroller.isConnected = false;
            current = makeElements();
        },
        remove() {
            current.content.isConnected = false;
            current.scroller.isConnected = false;
            conversationPresent = false;
        },
    };
}

test("opens at the latest message and follows delayed photos", () => {
    const f = fixture({nested: true});
    f.render(); f.frame();
    assert.equal(f.scroller.scrollTop, 1200);
    f.scroller.scrollHeight = 2000;
    f.content.dispatchEvent(new Event("load")); f.frame();
    assert.equal(f.scroller.scrollTop, 1600);
});

for (const event of ["wheel", "touchstart", "pointerdown", "keydown"]) {
    test(`${event} preserves history position during photo/resize and timer refreshes`, () => {
        const f = fixture();
        f.render(); f.frame();
        f.scroller.dispatchEvent(new Event(event));
        f.scroller.scrollTop = 200;
        f.frame();
        f.content.dispatchEvent(new Event("load"));
        f.resize();
        f.mutate();
        f.render(); f.frame();
        assert.equal(f.scroller.scrollTop, 200);
        assert.equal(f.observers.length, 1);
        assert.ok(!f.observers[0].disconnected);
    });
}

test("switching chat/page or refreshing the full page initializes the bottom again", () => {
    const f = fixture();
    for (const view of ["1:0:1", "2:0:1", "2:50:1", "2:50:2"]) {
        f.scroller.scrollTop = 50;
        f.render(view); f.frame();
        assert.equal(f.scroller.scrollTop, 1200);
    }
    assert.ok(f.observers.slice(0, -1).every(observer => observer.disconnected));
    assert.ok(f.mutations.slice(0, -1).every(observer => observer.disconnected));
});

test("follows repeated incoming and outgoing updates without expiring or registering twice", () => {
    const f = fixture();
    f.render(); f.frame();
    for (const height of [1800, 2200, 2600]) {
        f.scroller.scrollHeight = height;
        f.render(); f.mutate(); f.frame();
        assert.equal(f.scroller.scrollTop, height - 400);
    }
    assert.equal(f.observers.length, 1);
    assert.equal(f.mutations.length, 1);
});

test("a render-induced reset to the top does not disable following new messages", () => {
    const f = fixture();
    f.render(); f.frame();
    f.scroller.scrollHeight = 2000;
    f.scroller.scrollTop = 0;
    f.scroller.dispatchEvent(new Event("scroll"));
    f.mutate(); f.frame();
    assert.equal(f.scroller.scrollTop, 1600);
});

test("temporary empty history during rerender still ends at the newest message", () => {
    const f = fixture();
    f.render(); f.frame();
    f.scroller.scrollHeight = 0;
    f.scroller.scrollTop = 0;
    f.scroller.dispatchEvent(new Event("scroll"));
    f.mutate(); f.frame();
    f.scroller.scrollHeight = 2200;
    f.scroller.dispatchEvent(new Event("scroll"));
    f.mutate(); f.frame();
    assert.equal(f.scroller.scrollTop, 1800);
});

test("reattaches to a replacement thread without waiting for the script to rerun", () => {
    const f = fixture({nested: true});
    f.render(); f.frame();
    const previous = f.scroller;
    f.replace();
    f.scroller.scrollHeight = 2400;
    f.mutate(); f.frame();
    assert.equal(f.scroller.scrollTop, 2000);
    assert.notEqual(f.scroller, previous);
    f.scroller.scrollHeight = 2600;
    f.resize(); f.frame();
    assert.equal(f.scroller.scrollTop, 2200);
    assert.equal(f.mutations.length, 1);
});

test("restores the reader's position after a framework reset or thread replacement", () => {
    const f = fixture();
    f.render(); f.frame();
    f.userScroll(300);
    f.scroller.scrollTop = 0;
    f.scroller.dispatchEvent(new Event("scroll"));
    f.mutate(); f.frame();
    assert.equal(f.scroller.scrollTop, 300);
    f.replace(); f.mutate(); f.frame();
    assert.equal(f.scroller.scrollTop, 300);
});

test("a temporarily missing thread does not stop following when it reappears", () => {
    const f = fixture();
    f.render(); f.frame();
    f.content.isConnected = false;
    f.mutate(); f.frame();
    assert.ok(!f.mutations[0].disconnected);
    f.replace(); f.mutate(); f.frame();
    assert.equal(f.scroller.scrollTop, 1200);
});

test("resumes following after returning to the bottom", () => {
    const f = fixture();
    f.render(); f.frame();
    f.userScroll(100);
    f.scroller.scrollHeight = 2000;
    f.mutate(); f.frame();
    assert.equal(f.scroller.scrollTop, 100);
    f.userScroll(1600);
    f.scroller.scrollHeight = 2400;
    f.resize(); f.frame();
    assert.equal(f.scroller.scrollTop, 2000);
});

test("clicking without scrolling does not disable following", () => {
    const f = fixture();
    f.render(); f.frame();
    f.scroller.dispatchEvent(new Event("pointerdown")); f.frame();
    f.scroller.scrollHeight = 2000;
    f.mutate(); f.frame();
    assert.equal(f.scroller.scrollTop, 1600);
});

test("sending a reply reopens the bottom and follows its later webhook", () => {
    const f = fixture();
    f.render(); f.frame();
    f.userScroll(100);
    f.render("1:0:2"); f.frame();
    assert.equal(f.scroller.scrollTop, 1200);
    f.scroller.scrollHeight = 2000;
    f.mutate(); f.frame();
    assert.equal(f.scroller.scrollTop, 1600);
});

test("disconnects observers when the chat is removed", () => {
    const f = fixture({nested: true});
    f.render(); f.frame();
    f.remove();
    f.resize(); f.frame();
    assert.ok(f.observers[0].disconnected);
    assert.ok(f.mutations[0].disconnected);
});

test("user input cancels even the first queued scroll frame", () => {
    const f = fixture();
    f.render();
    f.scroller.dispatchEvent(new Event("wheel"));
    f.scroller.scrollTop = 75;
    f.frame();
    assert.equal(f.scroller.scrollTop, 75);
});
