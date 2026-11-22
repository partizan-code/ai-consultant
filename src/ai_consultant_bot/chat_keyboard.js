// Submit only the focused chat editor; Shift+Enter keeps a line break.
(() => {
    window.aiConsultantChatKeyboard?.();
    const onKeyDown = event => {
        const editor = event.target;
        if (event.key !== "Enter" || event.isComposing || event.keyCode === 229
            || editor?.tagName !== "TEXTAREA") return;
        const composer = editor.closest(".st-key-reply_composer");
        if (!composer) return;
        const modified = event.ctrlKey || event.metaKey || event.altKey;
        if (event.shiftKey && !modified) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        if (modified || event.repeat || editor.disabled || !editor.value.trim()) return;
        const button = composer.querySelector('[class*="st-key-reply_send_"] button');
        if (!button || button.disabled) return;
        // Blur commits Streamlit's latest textarea value before the button trigger.
        editor.blur();
        button.click();
    };
    document.addEventListener("keydown", onKeyDown, true);
    window.aiConsultantChatKeyboard = () => {
        document.removeEventListener("keydown", onKeyDown, true);
    };
})();
