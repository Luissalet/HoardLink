#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use base64::{engine::general_purpose::STANDARD, Engine};
use reqwest::{Client, StatusCode, Url};
use serde_json::{json, Value};
use std::{
    io::{self, Write},
    path::{Path, PathBuf},
    time::Duration,
};
use tauri::{async_runtime::Mutex, Manager, State};

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
    let Some((id, index)) = path.split_once('?') else {
        return piece_file(path);
    };
    piece_file(id) && index.strip_prefix("preview=").is_some_and(digits)
}

fn piece_file(path: &str) -> bool {
    let Some(id) = path.strip_prefix("/api/agora/piece_file/") else {
        return false;
    };
    digits(id)
}

fn digits(value: &str) -> bool {
    !value.is_empty() && value.bytes().all(|c| c.is_ascii_digit())
}

enum HubInitError {
    InvalidOrigin,
    NonLocalOrigin,
    Client(reqwest::Error),
}

impl HubInitError {
    fn message(&self, lang: &str) -> String {
        match self {
            Self::InvalidOrigin => text(lang, "Dirección del Hub no válida", "Invalid Hub address").into(),
            Self::NonLocalOrigin => text(lang, "El Hub debe estar en una dirección local", "The Hub must use a local address").into(),
            Self::Client(error) => error.to_string(),
        }
    }
}

fn hub_origin(base: &str) -> Result<Url, HubInitError> {
    let url = Url::parse(base).map_err(|_| HubInitError::InvalidOrigin)?;
    if url.scheme() != "http"
        || !matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "[::1]"))
        || !url.username().is_empty()
        || url.password().is_some()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(HubInitError::NonLocalOrigin);
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

fn text(lang: &str, es: &'static str, en: &'static str) -> &'static str {
    if lang == "en" {
        en
    } else {
        es
    }
}

fn session_error(lang: &str) -> &'static str {
    text(
        lang,
        "No se ha podido abrir la sesión de escritura. Vuelve a abrir la consola del Ágora.",
        "The write session could not be opened. Reopen the Agora console.",
    )
}

struct Hub {
    client: Client,
    origin: String,
    session: Mutex<Option<String>>,
}

impl Hub {
    fn new() -> Result<Self, HubInitError> {
        let base =
            std::env::var("AGORA_HUB_URL").unwrap_or_else(|_| "http://127.0.0.1:8810".into());
        let origin = hub_origin(&base)?.origin().ascii_serialization();
        let client = Client::builder()
            .timeout(Duration::from_secs(30))
            .no_proxy()
            .user_agent("HoardAgora")
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(HubInitError::Client)?;
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
    hub: State<'_, Result<Hub, HubInitError>>,
    lang: String,
) -> Result<(), String> {
    let hub = hub.inner().as_ref().map_err(|error| error.message(&lang))?;
    hub.session(&lang, None).await.map(|_| ())
}

// Only local coordination routes are relayed. Proofs and launcher credentials stay in this process.
#[tauri::command]
async fn hub_request(
    hub: State<'_, Result<Hub, HubInitError>>,
    path: String,
    body: Option<Value>,
    lang: String,
) -> Result<Value, String> {
    if !valid_agora_path(&path) {
        return Err(text(&lang, "Ruta de coordinación no válida", "Invalid coordination path").into());
    }
    let hub = hub.inner().as_ref().map_err(|error| error.message(&lang))?;
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
            .map_err(|_| {
                text(&lang, "No se puede conectar con el Hub local", "Could not connect to the local Hub")
            })?;
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
                _ => {
                    return Err(text(&lang, "El Hub no devolvió una imagen", "The Hub did not return an image").into());
                }
            };
            let bytes = response.bytes().await.map_err(|e| e.to_string())?;
            return Ok(
                json!({ "image": format!("data:{mime};base64,{}", STANDARD.encode(bytes)) }),
            );
        }
        let value: Value = response
            .json()
            .await
            .map_err(|_| {
                text(&lang, "El Hub devolvió una respuesta no válida", "The Hub returned an invalid response")
            })?;
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
            .unwrap_or(text(&lang, "La operación falló", "The operation failed"))
            .into());
    }
    unreachable!()
}

#[tauri::command]
async fn open_hub(
    hub: State<'_, Result<Hub, HubInitError>>,
    space_id: Option<String>,
    decision: Option<String>,
    lang: String,
) -> Result<(), String> {
    let hub = hub.inner().as_ref().map_err(|error| error.message(&lang))?;
    let mut url = hub_origin(&hub.origin).map_err(|error| error.message(&lang))?;
    if let Some(id) = space_id {
        url.query_pairs_mut().append_pair("space_id", &id);
    }
    let view = if let Some(key) = decision {
        if !decision_key(&key) {
            return Err(text(&lang, "Enlace a decisión no válido", "Invalid decision link").into());
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
        .map_err(|_| text(&lang, "No se puede abrir Hoard Hub", "Could not open Hoard Hub"))?;
    Ok(())
}

fn download_name(filename: &str) -> Option<&Path> {
    let path = Path::new(filename);
    if path.file_name()?.to_str()? != filename
        || filename
            .chars()
            .any(|c| c.is_control() || r#"\/:*?"<>|"#.contains(c))
        || filename.ends_with(['.', ' '])
    {
        return None;
    }
    Some(path)
}

fn save_download(
    mut file: tempfile::NamedTempFile,
    directory: &Path,
    name: &Path,
) -> io::Result<String> {
    let stem = name.file_stem().unwrap_or_default().to_string_lossy();
    let ext = name
        .extension()
        .map(|ext| format!(".{}", ext.to_string_lossy()))
        .unwrap_or_default();
    let mut index = 0;
    loop {
        let filename = if index == 0 {
            name.to_string_lossy().into_owned()
        } else {
            format!("{stem} ({index}){ext}")
        };
        match file.persist_noclobber(directory.join(&filename)) {
            Ok(_) => return Ok(filename),
            Err(error) if error.error.kind() == io::ErrorKind::AlreadyExists => {
                file = error.file;
                index += 1;
            }
            Err(error) => return Err(error.error),
        }
    }
}

#[tauri::command]
async fn open_piece(
    hub: State<'_, Result<Hub, HubInitError>>,
    app: tauri::AppHandle,
    path: String,
    filename: Option<String>,
    lang: String,
) -> Result<Option<String>, String> {
    if !piece_file(&path) {
        return Err(text(&lang, "Ruta de pieza de producción no válida", "Invalid production piece path").into());
    }
    let hub = hub.inner().as_ref().map_err(|error| error.message(&lang))?;
    let url = format!("{}{path}", hub.origin);
    let Some(filename) = filename else {
        std::process::Command::new("explorer.exe")
            .arg(url)
            .spawn()
            .map_err(|error| error.to_string())?;
        return Ok(None);
    };
    let name = download_name(&filename)
        .ok_or_else(|| text(&lang, "Nombre de fichero no válido", "Invalid file name"))?;
    let directory = app
        .path()
        .download_dir()
        .map_err(|error| error.to_string())?;
    let mut response = hub
        .client
        .get(&url)
        .header("Origin", &hub.origin)
        .header("Sec-Fetch-Site", "same-origin")
        .send()
        .await
        .and_then(|reply| reply.error_for_status())
        .map_err(|error| error.to_string())?;
    let mut file =
        tempfile::NamedTempFile::new_in(&directory).map_err(|error| error.to_string())?;
    while let Some(bytes) = response.chunk().await.map_err(|error| error.to_string())? {
        file.write_all(&bytes).map_err(|error| error.to_string())?;
    }
    file.as_file()
        .sync_all()
        .map_err(|error| error.to_string())?;
    save_download(file, &directory, name)
        .map(Some)
        .map_err(|error| error.to_string())
}

fn main() {
    tauri::Builder::default()
        .manage(Hub::new())
        .invoke_handler(tauri::generate_handler![
            open_write_session,
            hub_request,
            open_hub,
            open_piece
        ])
        .run(tauri::generate_context!())
        .expect("Could not start the coordination window");
}

#[cfg(test)]
mod tests {
    use super::{
        decision_key, download_name, hub_origin, piece_preview, save_download, valid_agora_path,
    };
    use std::io::Write;

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
        assert!(piece_preview("/api/agora/piece_file/12?preview=3"));
        assert!(!piece_preview("/api/agora/piece_file/12?preview="));
        assert!(!piece_preview(
            "/api/agora/piece_file/12?preview=3&inline=1"
        ));
        assert!(!piece_preview("/api/agora/piece_file/12/index.html"));
    }

    #[test]
    fn downloads_keep_the_original_extension_and_leave_existing_files() {
        let dir = tempfile::tempdir().unwrap();
        let name = download_name("Informe ñ.docx").unwrap();
        std::fs::write(dir.path().join(name), b"existing").unwrap();
        let mut file = tempfile::NamedTempFile::new_in(dir.path()).unwrap();
        file.write_all(b"revision").unwrap();
        let saved = save_download(file, dir.path(), name).unwrap();
        assert_eq!(saved, "Informe ñ (1).docx");
        assert_eq!(std::fs::read(dir.path().join(saved)).unwrap(), b"revision");
        assert_eq!(std::fs::read(dir.path().join(name)).unwrap(), b"existing");
        for bad in [
            "../report.docx",
            r"..\report.docx",
            r"C:\report.docx",
            "build:report",
            "report.",
        ] {
            assert!(download_name(bad).is_none(), "{bad}");
        }
    }
}
