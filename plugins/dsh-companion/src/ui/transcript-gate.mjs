/**
 * Gate for anything the recognizer hands back. Noisy rooms produce short
 * fragments, punctuation, or foreign script; those must never be sent as if the
 * user had said them.
 */
const HAN = /[\u3400-\u4DBF\u4E00-\u9FFF]/gu;

export function checkTranscript(text, {
  requireChinese = true,
  minChars = 2,
  chineseRatio = 0.3,
} = {}) {
  const clean = String(text ?? '').replace(/[\s\p{P}\p{S}]/gu, '');
  const chars = [...clean];
  if (chars.length < minChars) return {ok: false, reason: 'too-short', chars: chars.length};
  const han = (clean.match(HAN) ?? []).length;
  if (requireChinese && han === 0) return {ok: false, reason: 'no-chinese', chars: chars.length, han};
  if (requireChinese && han / chars.length < chineseRatio) {
    return {ok: false, reason: 'mostly-non-chinese', chars: chars.length, han};
  }
  return {ok: true, chars: chars.length, han};
}
