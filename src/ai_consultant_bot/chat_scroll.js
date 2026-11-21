// Follow new messages while at the bottom; preserve the position when reading history.
(() => {
    const view = "__CHAT_VIEW__";
    const previous = window.aiConsultantChatScroll;
    if (previous?.view === view) {
        previous.refresh();
        return;
    }
    previous?.cleanup();

    let content;
    let scroller;
    let active = true;
    let following = true;
    let lastTop = 0;
    let userInput = false;
    let frame;
    let inputFrame;
    let inputTimeout;
    const events = ["wheel", "touchstart", "pointerdown", "keydown"];
    const findScroller = element => {
        let fallback;
        while (element) {
            if (["auto", "scroll"].includes(getComputedStyle(element).overflowY)) {
                fallback ??= element;
                if (element.scrollHeight > element.clientHeight + 1) return element;
            }
            if (element.classList?.contains("st-key-conversation_window")) break;
            element = element.parentElement;
        }
        return fallback;
    };
    const updatePosition = () => {
        if (!active || !scroller?.isConnected) return;
        lastTop = scroller.scrollTop;
        following = scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop <= 48;
        if (!following) cancelAnimationFrame(frame);
    };
    const endInputLater = () => {
        clearTimeout(inputTimeout);
        inputTimeout = setTimeout(() => {
            userInput = false;
            refresh();
        }, 250);
    };
    const onScroll = () => {
        // DOM replacement, layout and scroll anchoring also emit scroll events.
        // Only a recent user gesture may change whether we follow the bottom.
        if (!userInput) return;
        updatePosition();
        endInputLater();
    };
    const pause = () => {
        userInput = true;
        following = false;
        cancelAnimationFrame(frame);
        cancelAnimationFrame(inputFrame);
        // A click without scrolling must not disable following future messages.
        inputFrame = requestAnimationFrame(updatePosition);
        endInputLater();
    };
    const detach = () => {
        scroller?.removeEventListener("scroll", onScroll);
        for (const name of events) scroller?.removeEventListener(name, pause, true);
        content?.removeEventListener("load", refresh, true);
        content?.removeEventListener("error", refresh, true);
    };
    const bind = () => {
        const nextContent = document.querySelector(".st-key-chat_thread");
        if (!nextContent?.isConnected) return false;
        const nextScroller = findScroller(nextContent);
        if (!nextScroller) return false;
        if (content !== nextContent || scroller !== nextScroller) {
            detach();
            resizeObserver.disconnect();
            content = nextContent;
            scroller = nextScroller;
            scroller.addEventListener("scroll", onScroll, {passive: true});
            for (const name of events) {
                scroller.addEventListener(name, pause, {capture: true, passive: true});
            }
            content.addEventListener("load", refresh, true);
            content.addEventListener("error", refresh, true);
        }
        resizeObserver.observe(scroller);
        for (const child of scroller.children) resizeObserver.observe(child);
        return true;
    };
    const refresh = () => {
        if (!active) return;
        cancelAnimationFrame(frame);
        frame = requestAnimationFrame(() => {
            if (!active) return;
            if (!bind()) {
                // The thread can disappear temporarily while its fragment is redrawn.
                if (!document.querySelector(".st-key-conversation_window")) cleanup();
                return;
            }
            if (following) scroller.scrollTop = scroller.scrollHeight;
            else if (!userInput) scroller.scrollTop = lastTop;
        });
    };
    const resizeObserver = new ResizeObserver(refresh);
    // Observe a stable ancestor: Streamlit can replace the entire scrolling element.
    const mutationObserver = new MutationObserver(refresh);
    const cleanup = () => {
        active = false;
        cancelAnimationFrame(frame);
        cancelAnimationFrame(inputFrame);
        clearTimeout(inputTimeout);
        resizeObserver.disconnect();
        mutationObserver.disconnect();
        detach();
        if (window.aiConsultantChatScroll?.cleanup === cleanup) {
            delete window.aiConsultantChatScroll;
        }
    };
    window.aiConsultantChatScroll = {view, refresh, cleanup};
    mutationObserver.observe(document.body, {childList: true, subtree: true, characterData: true});
    bind();
    refresh();
})();
