// Initialize each chat/history page once. Never scroll against user input.
(() => {
    const view = "__CHAT_VIEW__";
    const content = document.querySelector(".st-key-chat_thread");
    if (!content) return;
    let scroller = content;
    while (scroller && !["auto", "scroll"].includes(getComputedStyle(scroller).overflowY)) {
        scroller = scroller.parentElement;
    }
    if (!scroller || scroller.dataset.chatScrollView === view) return;
    scroller.chatScrollCleanup?.();
    scroller.dataset.chatScrollView = view;

    let active = true;
    let frame;
    let timeout;
    const events = ["wheel", "touchstart", "pointerdown", "keydown"];
    const follow = () => {
        if (!active) return;
        cancelAnimationFrame(frame);
        frame = requestAnimationFrame(() => {
            if (active && scroller.isConnected) scroller.scrollTop = scroller.scrollHeight;
        });
    };
    const observer = new ResizeObserver(follow);
    const images = [...content.querySelectorAll("img")];
    const stop = () => {
        active = false;
        cancelAnimationFrame(frame);
        clearTimeout(timeout);
        observer.disconnect();
        for (const name of events) scroller.removeEventListener(name, stop, true);
        for (const image of images) {
            image.removeEventListener("load", follow);
            image.removeEventListener("error", follow);
        }
    };
    scroller.chatScrollCleanup = stop;
    for (const name of events) scroller.addEventListener(name, stop, {capture: true, passive: true});
    for (const child of scroller.children) observer.observe(child);
    for (const image of images) {
        image.addEventListener("load", follow);
        image.addEventListener("error", follow);
    }
    // Bound initialization even when a remote image never finishes loading.
    timeout = setTimeout(stop, 10000);
    follow();
})();
