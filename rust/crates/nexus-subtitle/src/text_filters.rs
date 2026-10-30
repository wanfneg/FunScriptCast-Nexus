//! 文本过滤器（对齐 `vendor/subtitle/text_filters.py`）：拉丁幻觉判定与
//! token 拼接。`has_repetition_loop`/`strip_wrap_quotes` 在 `stream.rs`。

/// 日语字符（假名 + 汉字）：判断"有没有日文字符"用。
fn is_ja_char(c: char) -> bool {
    matches!(c as u32,
        0x3041..=0x309F   // 平假名
        | 0x30A1..=0x30FF // 片假名
        | 0x4E00..=0x9FFF // CJK 统一汉字
    )
}

/// 日语音频里输出"一个日文字符都没有"的**短**文本（≤16 字）→ 判为幻觉。
///
/// 实测（R44 整片 A/B）：Whisper 系在日语短块/噪声上会吐 `.` / `Thank` / `I` /
/// `you` 并当台词上屏；日语外来语写片假名不写拉丁字母，这类短输出不可能是
/// 真实台词。长段纯英文属于另一类问题，不在这里一刀切。
pub fn is_latin_hallucination(text: &str, lang_key: &str) -> bool {
    // 对齐 Python 原版门控（text_filters.py 的防误杀三限制之一）：只对 ja 生效，
    // en/ko 源语言本来就该是拉丁字母。门控内置在本函数里——本文件是"两边必须
    // 一致"判据的唯一实现，不能依赖每个调用方记得先判语言。
    if !lang_key.to_lowercase().starts_with("ja") {
        return false;
    }
    const MAX_CHARS: usize = 16;
    let t = text.trim();
    if t.is_empty() || t.chars().count() > MAX_CHARS {
        return false;
    }
    !t.chars().any(is_ja_char)
}

/// 拼接 ASR token：中日文直接相连，英文/数字之间补空格。
///
/// 直接相加在英文上会粘成一坨（"hello"+"world" → "helloworld"），而中日文
/// 之间**不能**加空格（加了字幕会多出空格）。
pub fn join_tokens(parts: &[String]) -> String {
    let mut out = String::new();
    for t in parts {
        let t = t.as_str();
        if t.is_empty() {
            continue;
        }
        let prev_alnum = out
            .chars()
            .last()
            .map(|c| c.is_ascii_alphanumeric())
            .unwrap_or(false);
        let next_alnum = t.chars().next().map(|c| c.is_ascii_alphanumeric()).unwrap_or(false);
        if prev_alnum && next_alnum {
            out.push(' ');
        }
        out.push_str(t);
    }
    out
}

/// 去掉标点/空白后的"净文本"（行长合并的长度判据用，对齐 Python `_lm_strip`）。
pub fn strip_punct(text: &str) -> String {
    text.chars()
        .filter(|c| !matches!(c,
            '。' | '．' | '！' | '？' | '!' | '?' | '…' | '、' | '，' | ',' | ' '
            | '\u{3000}' | '「' | '」' | '『' | '』' | '（' | '）' | '-'
        ))
        .collect()
}

/// 短叹词判重的归一化（对齐 Python `_apply_hallucination_gate` 的 re.sub）。
pub fn normalize_for_dedup(text: &str) -> String {
    text.chars()
        .filter(|c| !matches!(c,
            '、' | '。' | '！' | '？' | ' ' | '\u{3000}' | '．' | '，' | ','
            | '.' | '!' | '?' | '…' | '「' | '」' | '『' | '』' | '（' | '）' | '・'
        ))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn latin_hallucination_detection() {
        assert!(is_latin_hallucination(".", "ja"));
        assert!(is_latin_hallucination("Thank", "ja"));
        assert!(is_latin_hallucination("I", "ja"));
        assert!(is_latin_hallucination("2", "ja"));
        assert!(!is_latin_hallucination("こんにちは", "ja"));
        assert!(!is_latin_hallucination("OK、それで", "ja"));
        // 长段纯英文不判（另一类问题）
        assert!(!is_latin_hallucination(&"a".repeat(20), "ja"));
        // 非 ja 语言无条件放行：en/ko 源语言本就该是拉丁字母
        assert!(!is_latin_hallucination("Thank", "en"));
    }

    #[test]
    fn join_tokens_spaces_only_between_ascii() {
        assert_eq!(join_tokens(&["こんにちは".into(), "世界".into()]), "こんにちは世界");
        assert_eq!(join_tokens(&["hello".into(), "world".into()]), "hello world");
        assert_eq!(join_tokens(&["2024".into(), "年".into()]), "2024年");
    }

    #[test]
    fn strip_and_normalize() {
        assert_eq!(strip_punct("もっと。"), "もっと");
        assert_eq!(normalize_for_dedup("はい、。"), "はい");
    }
}
