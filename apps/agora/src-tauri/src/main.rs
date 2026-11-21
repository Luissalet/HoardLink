#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use base64::{engine::general_purpose::STANDARD, Engine};
use reqwest::{Client, StatusCode, Url};
use serde_json::{json, Value};
use std::{path::PathBuf, time::Duration};
use tauri::{async_runtime::Mutex, State};

const SESSION_HEADER: &str = "X-Hoard-UI-Session";
const SESSION_REQUIRED: &str = "hub_ui_session_required";

fn valid_agora_path(path: &str) -> bool {
    let route = path.split('?').next().unwrap_or("");
    route.starts_with("/api/agora/")
        && !path.contains('#')
        && route
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'/' | b'-' | b'_'))
}

fn decision_key(value: &str) -> bool {
    value.split_once('-').is_some_and(|(kind, id)| {
        matches!(kind, "gate" | "draft") && !id.is_empty() && id.bytes().all(|c| c.is_ascii_digit())
    })
}

fn piece_preview(path: &str) -> bool {
    let Some(piece) = path.strip_prefix("/api/agora/piece_file/") else {
        return false;
    };
    let id = piece.strip_suffix("?preview=0").unwrap_or(piece);
    !id.is_empty() && id.bytes().all(|c| c.is_ascii_digit())
}

fn hub_origin(base: &str) -> Result<Url, String> {
    let url = Url::parse(base).map_err(|_| "Dirección del Hub no válida")?;
    if url.scheme() != "http"
        || !matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "[::1]"))
        || !url.username().is_empty()
        || url.password().is_some()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err("El Hub debe estar en una dirección local".into());
    }
    Ok(url)
}

fn launch_key_path() -> Option<PathBuf> {
    if let Some(dir) = std::env::var_os("HOARD_HUB_DATA_DIR") {
        return Some(PathBuf::from(dir).join("hub-ui-launch-token"));
    }
    // Direct launches from the installed bin or from the default Cargo output.
    let exe = std::env::current_exe().ok()?;
    for path in exe.ancestors() {
        if let Some(apps) = path.parent() {
            if path.ends_with("agora") && apps.ends_with("apps") {
                return Some(apps.parent()?.join("data").join("hub-ui-launch-token"));
            }
        }
    }
    None
}

fn session_error(lang: &str) -> &'static str {
    if lang == "en" {
        "The write session could not be opened. Reopen the Agora console."
    } else {
        "No se ha podido abrir la sesión de escritura. Vuelve a abrir la consola del Ágora."
    }
}

struct Hub {
    client: Client,
    origin: String,
    session: Mutex<Option<String>>,
}

impl Hub {
    fn new() -> Result<Self, String> {
        let base =
            std::env::var("AGORA_HUB_URL").unwrap_or_else(|_| "http://127.0.0.1:8810".into());
        let origin = hub_origin(&base)?.origin().ascii_serialization();
        let client = Client::builder()
            .timeout(Duration::from_secs(30))
            .no_proxy()
            .user_agent("HoardAgora")
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|error| error.to_string())?;
        Ok(Self {
            client,
            origin,
            session: Mutex::new(None),
        })
    }

    async fn launch_ticket(&self, lang: &str) -> Result<String, String> {
        let error = session_error(lang);
        let path = launch_key_path().ok_or(error)?;
        let key = std::fs::read_to_string(path).map_err(|_| error)?;
        let reply: Value = self
            .client
            .post(format!("{}/api/ui/launch", self.origin))
            .bearer_auth(key.trim())
            .json(&json!({}))
            .send()
            .await
            .map_err(|_| error)?
            .error_for_status()
            .map_err(|_| error)?
            .json()
            .await
            .map_err(|_| error)?;
        reply["ticket"]
            .as_str()
            .map(str::to_owned)
            .ok_or_else(|| error.into())
    }

    async fn session(&self, lang: &str, rejected: Option<&str>) -> Result<String, String> {
        let mut cached = self.session.lock().await;
        if let Some(proof) = cached.as_ref() {
            if Some(proof.as_str()) != rejected {
                return Ok(proof.clone());
            }
        }
        *cached = None;
        let error = session_error(lang);
        let ticket = self.launch_ticket(lang).await?;
        let reply: Value = self
            .client
            .post(format!("{}/api/ui/session", self.origin))
            .header("Origin", &self.origin)
            .json(&json!({ "ticket": ticket }))
            .send()
            .await
            .map_err(|_| error)?
            .error_for_status()
            .map_err(|_| error)?
            .json()
            .await
            .map_err(|_| error)?;
        let proof = reply["session"].as_str().ok_or(error)?.to_owned();
        *cached = Some(proof.clone());
        Ok(proof)
    }
}

#[tauri::command]
async fn open_write_session(
    hub: State<'_, Result<Hub, String>>,
    lang: String,
) -> Result<(), String> {
    let hub = hub.inner().as_ref().map_err(Clone::clone)?;
    hub.session(&lang, None).await.map(|_| ())
}

// Only local coordination routes are relayed. Proofs and launcher credentials stay in this process.
#[tauri::command]
async fn hub_request(
    hub: State<'_, Result<Hub, String>>,
    path: String,
    body: Option<Value>,
    lang: String,
) -> Result<Value, String> {
    if !valid_agora_path(&path) {
        return Err("Ruta de coordinación no válida".into());
    }
    let hub = hub.inner().as_ref().map_err(Clone::clone)?;
    let url = format!("{}{path}", hub.origin);
    let mut proof = match &body {
        Some(_) => Some(hub.session(&lang, None).await?),
        None => None,
    };
    let language = if lang == "en" { "en-GB" } else { "es-ES" };
    for attempt in 0..2 {
        let request = match (&body, &proof) {
            (Some(value), Some(proof)) => hub
                .client
                .post(&url)
                .json(value)
                .header(SESSION_HEADER, proof),
            _ => hub.client.get(&url),
        };
        let response = request
            .header("Origin", &hub.origin)
            .header("Sec-Fetch-Site", "same-origin")
            .header("Accept-Language", language)
            .send()
            .await
            .map_err(|_| "No se puede conectar con el Hub local".to_string())?;
        let status = response.status();
        if body.is_none() && status.is_success() && piece_preview(&path) {
            let mime = match response
                .headers()
                .get(reqwest::header::CONTENT_TYPE)
                .and_then(|v| v.to_str().ok())
            {
                Some(
                    t @ ("image/png" | "image/jpeg" | "image/webp" | "image/gif" | "image/bmp"
                    | "image/avif" | "image/svg+xml"),
                ) => t.to_owned(),
                _ => return Err("El Hub no devolvió una imagen".into()),
            };
            let bytes = response.bytes().await.map_err(|e| e.to_string())?;
            return Ok(
                json!({ "image": format!("data:{mime};base64,{}", STANDARD.encode(bytes)) }),
            );
        }
        let value: Value = response
            .json()
            .await
            .map_err(|_| "El Hub devolvió una respuesta no válida".to_string())?;
        if status.is_success() {
            return Ok(value);
        }
        if status == StatusCode::FORBIDDEN && value["code"] == SESSION_REQUIRED {
            if attempt == 0 && proof.is_some() {
                // The Hub rejects this code before dispatching the requested write.
                proof = Some(hub.session(&lang, proof.as_deref()).await?);
                continue;
            }
            return Err(session_error(&lang).into());
        }
        return Err(value["error"]
            .as_str()
            .unwrap_or("La operación falló")
            .into());
    }
    unreachable!()
}

#[tauri::command]
async fn open_hub(
    hub: State<'_, Result<Hub, String>>,
    space_id: Option<String>,
    decision: Option<String>,
    lang: String,
) -> Result<(), String> {
    let hub = hub.inner().as_ref().map_err(Clone::clone)?;
    let mut url = hub_origin(&hub.origin)?;
    if let Some(id) = space_id {
        url.query_pairs_mut().append_pair("space_id", &id);
    }
    let view = if let Some(key) = decision {
        if !decision_key(&key) {
            return Err("Enlace a decisión no válido".into());
        }
        url.query_pairs_mut().append_pair("decision", &key);
        "hub/orders"
    } else {
        "agora"
    };
    let ticket = hub.launch_ticket(&lang).await?;
    url.set_fragment(Some(&format!("hub-session={ticket}&view={view}")));
    std::process::Command::new("explorer.exe")
        .arg(url.as_str())
        .spawn()
        .map_err(|_| "No se puede abrir Hoard Hub".to_string())?;
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .manage(Hub::new())
        .invoke_handler(tauri::generate_handler![
            open_write_session,
            hub_request,
            open_hub
        ])
        .run(tauri::generate_context!())
        .expect("Could not start the coordination window");
}

#[cfg(test)]
mod tests {
    use super::{decision_key, hub_origin, piece_preview, valid_agora_path};

    #[test]
    fn relay_stays_inside_coordination_routes() {
        assert!(valid_agora_path("/api/agora/threads/32/recent?limit=60"));
        for path in [
            "https://example.com/api/agora/",
            "/api/agora/../agent/call",
            "/api/agora/%2e%2e/agent/call",
            "/api/agora/%2f../tokens",
            "/api/agora/board#fragment",
            "/api/agora/..\\tokens",
        ] {
            assert!(!valid_agora_path(path), "unexpected route: {path}");
        }
        assert!(hub_origin("http://127.0.0.1:8810").is_ok());
        for base in [
            "https://127.0.0.1",
            "http://localhost/?a=b",
            "http://localhost/#view",
            "http://remote.example",
            "http://user@localhost",
        ] {
            assert!(hub_origin(base).is_err(), "unexpected Hub origin: {base}");
        }
    }

    #[test]
    fn decision_links_and_previews_use_fixed_routes() {
        assert!(decision_key("draft-7"));
        assert!(decision_key("gate-10"));
        assert!(!decision_key("gate-10#agora"));
        assert!(piece_preview("/api/agora/piece_file/12"));
        assert!(piece_preview("/api/agora/piece_file/12?preview=0"));
        assert!(!piece_preview("/api/agora/piece_file/12/index.html"));
    }
}
