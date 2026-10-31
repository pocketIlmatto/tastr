//! The track card (tastr/engine/card.py), written as the same JSON as TrackCard.to_dict().

use crate::json::Json;

#[derive(Clone, Debug)]
pub struct Key {
    pub tonic: i64,
    pub mode: &'static str,
    pub scale: &'static str,
    pub camelot: String,
    pub confidence: f64,
    pub likely_correct: f64,
}

#[derive(Clone, Debug)]
pub struct Core {
    pub bpm: Option<f64>,
    pub bpm_confidence: Option<f64>,
    pub bpm_likely_correct: Option<f64>,
    pub key: Option<Key>,
    pub energy: Option<f64>,
    pub loudness_db: Option<f64>,
    pub beat_times: Vec<f64>,
    pub downbeat_times: Vec<f64>,
    pub beats_per_bar: i64,
    pub meter_set: bool,
}

impl Default for Core {
    fn default() -> Self {
        Core {
            bpm: None,
            bpm_confidence: None,
            bpm_likely_correct: None,
            key: None,
            energy: None,
            loudness_db: None,
            beat_times: Vec::new(),
            downbeat_times: Vec::new(),
            beats_per_bar: 4,
            meter_set: false,
        }
    }
}

#[derive(Clone, Debug)]
pub struct Section {
    pub start: f64,
    pub end: f64,
    pub label: String,
    pub letter: Option<String>,
    pub start_bar: Option<i64>,
    pub end_bar: Option<i64>,
    pub energy: Option<f64>,
    pub confidence: Option<f64>,
}

#[derive(Clone, Debug)]
pub struct Suggestion {
    pub category: &'static str,
    pub title: String,
    pub body: String,
    pub data: Json,
}

#[derive(Clone, Debug, Default)]
pub struct Card {
    pub duration: f64,
    pub sample_rate: i64,
    pub core: Core,
    pub sections: Vec<Section>,
    pub tags: Vec<(String, f64)>,
    pub suggestions: Vec<Suggestion>,
    /// name -> (times, values)
    pub series: Vec<(String, Vec<f64>, Vec<f64>)>,
    pub features: Vec<(String, f64)>,
    pub extras: Json,
    pub stages_run: Vec<(String, String)>,
    pub errors: Vec<(String, String)>,
}

impl Card {
    pub fn feature(&self, name: &str) -> Option<f64> {
        self.features.iter().find(|(k, _)| k == name).map(|(_, v)| *v)
    }

    pub fn set_feature(&mut self, name: &str, v: f64) {
        if let Some(slot) = self.features.iter_mut().find(|(k, _)| k == name) {
            slot.1 = v;
        } else {
            self.features.push((name.to_string(), v));
        }
    }

    pub fn energy_series(&self) -> Option<(&[f64], &[f64])> {
        self.series.iter().find(|(n, _, _)| n == "energy").map(|(_, t, v)| (t.as_slice(), v.as_slice()))
    }

    pub fn to_json(&self, source: Json) -> Json {
        let c = &self.core;
        let key = c.key.as_ref().map(|k| {
            Json::obj()
                .with("tonic", k.tonic)
                .with("mode", k.mode)
                .with("scale", k.scale)
                .with("camelot", k.camelot.clone())
                .with("confidence", k.confidence)
                .with("likely_correct", k.likely_correct)
        });
        let core = Json::obj()
            .with("bpm", c.bpm)
            .with("bpm_confidence", c.bpm_confidence)
            .with("bpm_likely_correct", c.bpm_likely_correct)
            .with("key", key.unwrap_or(Json::Null))
            .with("energy", c.energy)
            .with("loudness_db", c.loudness_db)
            .with("beat_times", c.beat_times.clone())
            .with("downbeat_times", c.downbeat_times.clone())
            .with("beats_per_bar", c.beats_per_bar)
            .with("meter_set", c.meter_set);
        let sections: Vec<Json> = self
            .sections
            .iter()
            .map(|s| {
                Json::obj()
                    .with("start", s.start)
                    .with("end", s.end)
                    .with("label", s.label.clone())
                    .with("letter", s.letter.clone())
                    .with("start_bar", s.start_bar)
                    .with("end_bar", s.end_bar)
                    .with("energy", s.energy)
                    .with("confidence", s.confidence)
            })
            .collect();
        let tags: Vec<Json> =
            self.tags.iter().map(|(t, s)| Json::obj().with("tag", t.clone()).with("score", *s)).collect();
        let suggestions: Vec<Json> = self
            .suggestions
            .iter()
            .map(|s| {
                Json::obj()
                    .with("category", s.category)
                    .with("title", s.title.clone())
                    .with("body", s.body.clone())
                    .with("data", s.data.clone())
            })
            .collect();
        let series = Json::Obj(
            self.series
                .iter()
                .map(|(n, t, v)| (n.clone(), Json::obj().with("times", t.clone()).with("values", v.clone())))
                .collect(),
        );
        let pairs =
            |v: &[(String, String)]| Json::Obj(v.iter().map(|(k, s)| (k.clone(), Json::from(s.clone()))).collect());
        Json::obj()
            .with("source", source)
            .with("duration", self.duration)
            .with("sample_rate", self.sample_rate)
            .with("core", core)
            .with("sections", Json::Arr(sections))
            .with("tags", Json::Arr(tags))
            .with("suggestions", Json::Arr(suggestions))
            .with("series", series)
            .with("features", Json::Obj(self.features.iter().map(|(k, v)| (k.clone(), Json::Num(*v))).collect()))
            .with("extras", if self.extras == Json::Null { Json::obj() } else { self.extras.clone() })
            .with("stages_run", pairs(&self.stages_run))
            .with("errors", pairs(&self.errors))
            .with("schema_version", 1i64)
    }
}
