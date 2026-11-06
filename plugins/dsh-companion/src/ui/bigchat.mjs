/** Full-screen companion page: her on the left, the conversation on the right. */
const ACTIVITY_TEXT = {listening: '在听', thinking: '在想', speaking: '在说', idle: '陪着'};

export function createBigChat({React, store, call, actions, Stage}) {
  const h = React.createElement;
  return function BigChat({sessionId}) {
    const state = React.useSyncExternalStore(store.subscribe, store.getSnapshot);
    const snap = React.useSyncExternalStore(call.subscribe, call.getSnapshot);
    const [draft, setDraft] = React.useState('');
    const [sending, setSending] = React.useState(false);
    const [error, setError] = React.useState('');
    const list = React.useRef(null);
    const open = !!state.bigchat;

    React.useEffect(() => {
      if (!open || !sessionId) return undefined;
      const events = new EventSource(`/companion/api/events?sessionId=${encodeURIComponent(sessionId)}`);
      events.onmessage = (event) => {
        let data;
        try { data = JSON.parse(event.data); } catch { return; }
        if (data.sessionId !== sessionId || typeof data.text !== 'string') return;
        const text = data.text.replace(/```[\s\S]*?```/g, '').trim();
        if (!text) return;
        store.set({messages: [...store.get().messages, {role: 'assistant', text, at: Date.now()}]});
      };
      return () => events.close();
    }, [open, sessionId]);

    React.useEffect(() => {
      if (list.current) list.current.scrollTop = list.current.scrollHeight;
    }, [state.messages.length, open]);

    if (!open) return null;

    const send = async () => {
      const text = draft.trim();
      if (!text || sending) return;
      setSending(true);
      try {
        await actions.sendText(text);
        setDraft('');
      } catch (error) {
        setError(String(error?.message ?? error));
      } finally {
        setSending(false);
      }
    };

    return h('div', {className: 'dsh-companion-bigchat', role: 'dialog', 'aria-label': '大屏聊天'},
      h('header', {className: 'dsh-companion-bigchat-bar'},
        h('strong', null, '陪着她'),
        h('span', {className: 'dsh-companion-bigchat-live'}, ACTIVITY_TEXT[state.activity] || '陪着'),
        h('button', {
          type: 'button',
          className: 'dsh-companion-bigchat-voice',
          onClick: () => {
            const run = snap.phase === 'ACTIVE' ? actions.end() : actions.begin('voice');
            Promise.resolve(run).catch(actions.reportError);
          },
        }, snap.phase === 'ACTIVE' ? '挂断语音' : '语音聊天'),
        h('button', {type: 'button', onClick: () => Promise.resolve(actions.chooseScreen()).catch(actions.reportError)}, '屏幕'),
        h('button', {type: 'button', onClick: () => store.set({bigchat: false, panel: 'settings'})}, '设置'),
        h('button', {type: 'button', onClick: () => store.set({bigchat: false})}, '缩小')),
      h('div', {className: 'dsh-companion-bigchat-body'},
        h('div', {className: 'dsh-companion-bigchat-stage'}, h(Stage)),
        h('div', {className: 'dsh-companion-bigchat-chat'},
          h('div', {className: 'dsh-companion-bigchat-list', ref: list},
            state.messages.length === 0
              && h('p', {className: 'dsh-companion-bigchat-empty'}, '这里还没有话。说点什么吧。'),
            state.messages.map((message, index) => h('div', {
              key: `${message.at}-${index}`,
              className: `dsh-companion-bigchat-msg ${message.role}`,
            }, message.text))),
          error && h('div', {className: 'dsh-companion-bigchat-error'}, error),
          h('div', {className: 'dsh-companion-bigchat-input'},
            h('textarea', {
              value: draft,
              rows: 2,
              placeholder: '和她说点什么…（Enter 发送，Shift+Enter 换行）',
              onChange: (event) => setDraft(event.target.value),
              onKeyDown: (event) => {
                if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); }
              },
            }),
            h('button', {type: 'button', disabled: sending || !draft.trim(), onClick: send}, sending ? '发送中' : '发给她')))));
  };
}
