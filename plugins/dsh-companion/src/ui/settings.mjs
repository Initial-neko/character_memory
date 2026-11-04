import {HELP, LABELS} from '../config.mjs';
import {post} from './orchestrator.mjs';

const GROUPS = [
  {title: '语音', fields: ['gsvEndpoint', 'gsvVoice', 'gsvSpeed', 'gsvLanguage', 'asrEndpoint', 'asrProtocol', 'autoSend', 'requireChinese', 'minTranscriptChars', 'transcriptChineseRatio']},
  {title: '断句', fields: ['endpointSilenceMs', 'endpointMinSpeechMs', 'endpointMaxSpeechMs', 'endpointCalibrationMs', 'endpointThreshold']},
  {title: '屏幕与视觉', fields: ['screenMaxEdge', 'screenQuality', 'visionEnabled', 'screenAutoRead', 'screenIntervalMs', 'screenMaxReads']},
  {title: '设备', fields: ['audioSource', 'localMicUrl', 'localMicPython', 'inputDeviceId']},
];

const NUMBERS = new Set([
  'gsvSpeed', 'screenMaxEdge', 'screenQuality',
  'endpointSilenceMs', 'endpointMinSpeechMs', 'endpointMaxSpeechMs', 'endpointCalibrationMs', 'endpointThreshold',
]);
const READONLY = new Set(['asrProtocol']);

function typed(key, value) {
  return typeof value === 'boolean' ? value : NUMBERS.has(key) ? Number(value) : value;
}

export function createSettings({React, store, request, actions}) {
  const h = React.createElement;

  return function Settings() {
    const state = React.useSyncExternalStore(store.subscribe, store.getSnapshot);
    const [draft, setDraft] = React.useState(null);
    const [voices, setVoices] = React.useState([]);
    const [testing, setTesting] = React.useState(false);

    React.useEffect(() => {
      let alive = true;
      actions.loadConfig().then((config) => { if (alive) setDraft({...config.value}); }).catch(actions.reportError);
      actions.loadVoices().then((list) => { if (alive) setVoices(list.voices ?? []); }).catch(() => {});
      return () => { alive = false; };
    }, []);

    if (!draft) return h('div', {className: 'dsh-companion-root'}, h('p', null, '读取配置…'));

    const patch = (key, raw) => setDraft({...draft, [key]: typed(key, raw)});
    const options = voices.includes(draft.gsvVoice) ? voices : [draft.gsvVoice, ...voices].filter(Boolean);

    async function save() {
      const config = await request('/config', {
        method: 'PATCH',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({expectedRevision: state.config.revision, changes: draft}),
      });
      store.set({config});
      actions.configure(config.revision);
    }

    async function preview() {
      setTesting(true);
      try { await actions.previewVoice(draft.gsvVoice); } catch (error) { actions.reportError(error); } finally { setTesting(false); }
    }

    function field(key) {
      let input;
      if (typeof draft[key] === 'boolean') {
        input = h('input', {
          type: 'checkbox', checked: draft[key], title: HELP[key],
          onChange: (event) => patch(key, event.target.checked),
        });
      } else if (key === 'audioSource') {
        input = h('select', {
          value: draft[key], title: HELP[key],
          onChange: (event) => patch(key, event.target.value),
        }, [
          h('option', {key: 'local', value: 'local'}, '本地设备服务（推荐）'),
          h('option', {key: 'browser', value: 'browser'}, '浏览器麦克风'),
        ]);
      } else if (key === 'gsvVoice' && options.length > 0) {
        input = h('select', {
          value: draft[key], title: HELP[key],
          onChange: (event) => patch(key, event.target.value),
        }, options.map((voice) => h('option', {key: voice, value: voice}, voice)));
      } else {
        input = h('input', {
          type: NUMBERS.has(key) ? 'number' : 'text',
          ...(NUMBERS.has(key) ? {step: key === 'endpointThreshold' ? '0.001' : '1'} : {}),
          value: draft[key], disabled: READONLY.has(key), title: HELP[key],
          onChange: (event) => patch(key, event.target.value),
        });
      }
      return h('label', {key, title: HELP[key]}, LABELS[key], input, h('small', null, HELP[key]));
    }

    return h('div', {className: 'dsh-companion-root'},
      h('form', {
        className: 'dsh-companion-config',
        onSubmit: (event) => { event.preventDefault(); save().catch(actions.reportError); },
      }, GROUPS.map(({title, fields}) => h('div', {key: title},
        h('p', {className: 'dsh-companion-group'}, title),
        fields.map(field)))),
      h('footer', null,
        h('button', {type: 'submit', title: '只存配置，不碰设备也不生成内容'}, '保存配置'),
        h('button', {
          type: 'button', disabled: testing, title: '用当前声线合成一句试听，会调用 GSV',
          onClick: () => preview(),
        }, testing ? '试听中…' : '试听声线'),
        h('button', {
          type: 'button',
          title: '只查 GSV 健康，不合成',
          onClick: () => request('/health', post({capability: 'gsv'}))
            .then((health) => store.set({status: health.ready ? 'GSV 健康检查通过' : `GSV 没就绪：${health.reason}`}))
            .catch(actions.reportError),
        }, '检查 GSV')),
      h('p', {className: 'dsh-companion-status', role: 'status'}, state.status),
      h('p', {className: 'dsh-companion-note'}, `声线列表来自 GSV 的 /health（当前 ${voices.length} 条）。character-* 是语音设计出来的声线，起来就自动出现在这里。`));
  };
}
