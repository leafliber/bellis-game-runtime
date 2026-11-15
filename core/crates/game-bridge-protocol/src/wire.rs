use std::collections::BTreeMap;
use std::fmt;
use std::str::FromStr;

use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::value::RawValue;

pub(crate) const MAX_SAFE_INTEGER: i64 = (1 << 53) - 1;

/// 控制消息解码失败。版本不兼容从已经关联的握手应答中单独报告。
#[derive(Debug)]
pub enum DecodeError {
    Json(serde_json::Error),
    InvalidField(&'static str),
    ExtraField,
    UnexpectedMethod,
    UnmatchedResponse,
    IncompatibleMajor(u16),
}

impl fmt::Display for DecodeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Json(error) => error.fmt(formatter),
            Self::InvalidField(field) => write!(formatter, "缺少或无效的字段 {field}"),
            Self::ExtraField => formatter.write_str("消息含额外字段"),
            Self::UnexpectedMethod => formatter.write_str("控制连接不接受此方向的方法"),
            Self::UnmatchedResponse => formatter.write_str("应答与在途控制请求不匹配"),
            Self::IncompatibleMajor(major) => write!(formatter, "不兼容的游戏桥主版本 {major}"),
        }
    }
}

impl std::error::Error for DecodeError {}

impl From<serde_json::Error> for DecodeError {
    fn from(error: serde_json::Error) -> Self {
        Self::Json(error)
    }
}

/// 保留每个字段的原始 JSON。重复键与 Bellis 的 JSON.parse 一样取最后一个值。
pub(crate) struct Object<'a>(BTreeMap<String, &'a RawValue>);

impl<'a> Object<'a> {
    pub(crate) fn read(raw: &'a [u8]) -> Result<Self, DecodeError> {
        Ok(Self(serde_json::from_slice(raw)?))
    }

    pub(crate) fn payload(raw: &'a RawValue) -> Result<Self, DecodeError> {
        Self::read(raw.get().as_bytes())
    }

    pub(crate) fn remove(&mut self, key: &str) -> Option<&'a RawValue> {
        self.0.remove(key)
    }

    pub(crate) fn take(&mut self, key: &'static str) -> Result<&'a RawValue, DecodeError> {
        self.remove(key).ok_or(DecodeError::InvalidField(key))
    }

    pub(crate) fn nullable(
        &mut self,
        key: &'static str,
    ) -> Result<Option<&'a RawValue>, DecodeError> {
        let raw = self.take(key)?;
        Ok((raw.get() != "null").then_some(raw))
    }

    pub(crate) fn field<T: DeserializeOwned>(
        &mut self,
        key: &'static str,
    ) -> Result<T, DecodeError> {
        serde_json::from_str(self.take(key)?.get()).map_err(|_| DecodeError::InvalidField(key))
    }

    pub(crate) fn integer<T: FromStr>(&mut self, key: &'static str) -> Result<T, DecodeError> {
        integer(self.take(key)?, key)
    }

    pub(crate) fn finish<T>(self, value: T) -> Result<T, DecodeError> {
        if self.0.is_empty() {
            Ok(value)
        } else {
            Err(DecodeError::ExtraField)
        }
    }
}

pub(crate) fn integer<T: FromStr>(raw: &RawValue, key: &'static str) -> Result<T, DecodeError> {
    let spelling = raw.get();
    // RawValue 已核对 JSON 语法。整数解析进一步拒绝小数、指数、布尔和字符串。
    if spelling == "-0" {
        return Err(DecodeError::InvalidField(key));
    }
    spelling.parse().map_err(|_| DecodeError::InvalidField(key))
}

pub(crate) fn is_safe_integer(value: i64) -> bool {
    (-MAX_SAFE_INTEGER..=MAX_SAFE_INTEGER).contains(&value)
}

/// JSON-RPC 编号。在响应关联中区分数字和文本。
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize)]
#[serde(transparent)]
pub struct RequestId(Id);

#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize)]
#[serde(untagged)]
enum Id {
    Number(i64),
    Text(String),
}

impl RequestId {
    #[must_use]
    pub fn number(value: i64) -> Option<Self> {
        is_safe_integer(value).then_some(Self(Id::Number(value)))
    }

    #[must_use]
    pub fn text(value: String) -> Option<Self> {
        (1..=128)
            .contains(&value.chars().count())
            .then_some(Self(Id::Text(value)))
    }

    pub(crate) fn decode(raw: &RawValue) -> Result<Self, DecodeError> {
        let id = if raw.get().starts_with('"') {
            Self::text(serde_json::from_str(raw.get())?)
        } else {
            Self::number(integer(raw, "id")?)
        };
        id.ok_or(DecodeError::InvalidField("id"))
    }
}
