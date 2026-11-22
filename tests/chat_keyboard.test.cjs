// Offline keyboard behavior, including editor commit order and rerender cleanup.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(
    path.join(__dirname, "../src/ai_consultant_bot/chat_keyboard.js"), "utf8"
);

function fixture() {
    const document = new EventTarget();
    const window = {};
    const actions = [];
    let committed = "old text";
    const button = {disabled: false, click() { actions.push(["send", committed]); }};
    const composer = {querySelector() { return button; }};
    const editor = {
        tagName: "TEXTAREA", disabled: false, value: "new text",
        closest() { return composer; },
        blur() { committed = this.value; actions.push(["blur"]); },
    };
    const context = vm.createContext({document, window});
    const render = () => vm.runInContext(script, context);
    render();
    return {
        editor, button, actions, render,
        press(properties = {}, target = editor) {
            const event = new Event("keydown", {cancelable: true});
            Object.defineProperties(event, Object.fromEntries(
                Object.entries({key: "Enter", ...properties, target})
                    .map(([key, value]) => [key, {value}])
            ));
            document.dispatchEvent(event);
            return event;
        },
    };
}

test("Enter commits current text and sends exactly once after rerenders", () => {
    const f = fixture();
    f.render(); f.render();
    assert.equal(f.press().defaultPrevented, true);
    assert.deepEqual(f.actions, [["blur"], ["send", "new text"]]);
});

test("Shift+Enter leaves the browser's line break untouched", () => {
    const f = fixture();
    assert.equal(f.press({shiftKey: true}).defaultPrevented, false);
    assert.deepEqual(f.actions, []);
});

for (const modifier of ["ctrlKey", "metaKey", "altKey"]) {
    test(`${modifier}+Enter cannot trigger the old form shortcut`, () => {
        const f = fixture();
        assert.equal(f.press({[modifier]: true}).defaultPrevented, true);
        assert.deepEqual(f.actions, []);
    });
}

for (const properties of [{isComposing: true}, {keyCode: 229}, {key: "a"}]) {
    test(`composition and other keys pass through: ${JSON.stringify(properties)}`, () => {
        const f = fixture();
        assert.equal(f.press(properties).defaultPrevented, false);
        assert.deepEqual(f.actions, []);
    });
}

test("repeats, empty input and disabled controls never send", () => {
    const f = fixture();
    f.press({repeat: true});
    f.editor.value = " \n "; f.press();
    f.editor.value = "message";
    f.editor.disabled = true; f.press();
    f.editor.disabled = false;
    f.button.disabled = true; f.press();
    assert.deepEqual(f.actions, []);
});

test("other fields and textareas outside the composer are unaffected", () => {
    const f = fixture();
    assert.equal(f.press({}, {tagName: "INPUT"}).defaultPrevented, false);
    f.editor.closest = () => null;
    assert.equal(f.press().defaultPrevented, false);
    assert.deepEqual(f.actions, []);
});
