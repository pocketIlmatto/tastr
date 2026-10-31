//! A small JSON value and writer for the card, so the crate needs no serialisation dependency. Objects keep
//! their insertion order, as Python dicts do.

#[derive(Clone, Debug, PartialEq, Default)]
pub enum Json {
    #[default]
    Null,
    Bool(bool),
    Int(i64),
    Num(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    pub fn obj() -> Json {
        Json::Obj(Vec::new())
    }

    /// Sets `key` (replacing an existing value in place, as a dict update does).
    pub fn set(&mut self, key: &str, value: impl Into<Json>) -> &mut Json {
        if let Json::Obj(items) = self {
            let value = value.into();
            if let Some(slot) = items.iter_mut().find(|(k, _)| k == key) {
                slot.1 = value;
            } else {
                items.push((key.to_string(), value));
            }
        }
        self
    }

    pub fn with(mut self, key: &str, value: impl Into<Json>) -> Json {
        self.set(key, value);
        self
    }

    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(items) => items.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn write(&self, out: &mut String) {
        match self {
            Json::Null => out.push_str("null"),
            Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Json::Int(i) => out.push_str(&i.to_string()),
            Json::Num(x) => {
                if x.is_finite() {
                    // Shortest representation that reads back to the same double; always with a "." or "e".
                    let s = format!("{x:?}");
                    out.push_str(&s);
                } else {
                    out.push_str("null");
                }
            }
            Json::Str(s) => write_str(s, out),
            Json::Arr(items) => {
                out.push('[');
                for (i, v) in items.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    v.write(out);
                }
                out.push(']');
            }
            Json::Obj(items) => {
                out.push('{');
                for (i, (k, v)) in items.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    write_str(k, out);
                    out.push_str(": ");
                    v.write(out);
                }
                out.push('}');
            }
        }
    }
}

impl std::fmt::Display for Json {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let mut s = String::new();
        self.write(&mut s);
        f.write_str(&s)
    }
}

fn write_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

impl From<f64> for Json {
    fn from(x: f64) -> Json {
        Json::Num(x)
    }
}
impl From<i64> for Json {
    fn from(x: i64) -> Json {
        Json::Int(x)
    }
}
impl From<usize> for Json {
    fn from(x: usize) -> Json {
        Json::Int(x as i64)
    }
}
impl From<i32> for Json {
    fn from(x: i32) -> Json {
        Json::Int(x as i64)
    }
}
impl From<bool> for Json {
    fn from(x: bool) -> Json {
        Json::Bool(x)
    }
}
impl From<&str> for Json {
    fn from(x: &str) -> Json {
        Json::Str(x.to_string())
    }
}
impl From<String> for Json {
    fn from(x: String) -> Json {
        Json::Str(x)
    }
}
impl<T: Into<Json>> From<Option<T>> for Json {
    fn from(x: Option<T>) -> Json {
        x.map_or(Json::Null, Into::into)
    }
}
impl<T: Into<Json>> From<Vec<T>> for Json {
    fn from(x: Vec<T>) -> Json {
        Json::Arr(x.into_iter().map(Into::into).collect())
    }
}
