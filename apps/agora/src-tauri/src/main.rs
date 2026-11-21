#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::Value;
use std::{path::PathBuf, time::Duration};

fn valid_agora_path(path: &str) -> bool {
    let route = path.split('?').next().unwrap_or("");
    route.starts_with("/api/agora/") && !path.contains('#') &&
        route.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'/' | b'-' | b'_'))
}

fn decision_key(value: &str) -> bool {
    value.split_once('-').is_some_and(|(kind, id)| {
        matches!(kind, "gate" | "draft") && !id.is_empty() && id.bytes().all(|c| c.is_ascii_digit())
    })
}

fn piece_preview(path: &str) -> bool {
    let Some(piece) = path.strip_prefix("/api/agora/piece_file/") else { return false; };
    let id = piece.strip_suffix("?preview=0").unwrap_or(piece);
    !id.is_empty() && id.bytes().all(|c| c.is_ascii_digit())
}

fn hub_client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder().timeout(Duration::from_secs(30)).no_proxy()
        .redirect(reqwest::redirect::Policy::none()).build().map_err(|e| e.to_string())
}

async fn launch_ticket(client: &reqwest::Client, base: &str) -> Result<String, String> {
    let data_dir = std::env::var_os("HOARD_HUB_DATA_DIR").map(PathBuf::from).unwrap_or_else(|| {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../data")
    });
    let error = "Abre el Hub desde su lanzador para hacer cambios. / Open the Hub from its launcher to make changes.";
    let key = std::fs::read_to_string(data_dir.join("hub-ui-launch-token")).map_err(|_| error)?;
    let reply: Value = client.post(format!("{base}/api/ui/launch")).bearer_auth(key.trim())
        .json(&serde_json::json!({})).send().await.map_err(|_| error)?
        .json().await.map_err(|_| error)?;
    reply["ticket"].as_str().map(str::to_owned).ok_or_else(|| error.into())
}

// The shell only relays the local coordination API; it exposes no arbitrary URL,
// shell command, credential file or filesystem operation to browser content.
#[tauri::command]
async fn hub_request(path: String, body: Option<Value>) -> Result<Value, String> {
    if !valid_agora_path(&path) {
        return Err("Ruta de coordinación no válida".into());
    }
    let base = std::env::var("AGORA_HUB_URL").unwrap_or_else(|_| "http://127.0.0.1:8810".into());
    let parsed = reqwest::Url::parse(&base).map_err(|_| "Dirección del Hub no válida")?;
    if parsed.scheme() != "http" || !matches!(parsed.host_str(), Some("127.0.0.1") | Some("localhost") | Some("[::1]")) || parsed.username() != "" || parsed.password().is_some() || parsed.path() != "/" {
        return Err("El Hub debe estar en una dirección local".into());
    }
    let base = base.trim_end_matches('/');
    let client = hub_client()?;
    let url = format!("{base}{path}");
    let mut request = if let Some(ref value) = body {
        let ticket = launch_ticket(&client, base).await?;
        let error = "No se pudo abrir la sesión del Hub. / Could not open the Hub session.";
        let reply: Value = client.post(format!("{base}/api/ui/session")).json(&serde_json::json!({"ticket": ticket}))
            .send().await.map_err(|_| error)?.json().await.map_err(|_| error)?;
        client.post(url).json(value).header("X-Hoard-UI-Session", reply["session"].as_str().ok_or(error)?)
    } else { client.get(url) };
    request = request.header("Origin",base.trim_end_matches('/')).header("Sec-Fetch-Site","same-origin");
    let response = request.send().await.map_err(|_| "No se puede conectar con el Hub local".to_string())?;
    let status = response.status();
    if body.is_none() && status.is_success() && piece_preview(&path) {
        let mime = response.headers().get(reqwest::header::CONTENT_TYPE).and_then(|v| v.to_str().ok());
        let mime = match mime {
            Some(t @ ("image/png" | "image/jpeg" | "image/webp" | "image/gif" | "image/bmp" | "image/avif" | "image/svg+xml")) => t.to_owned(),
            _ => return Err("El Hub no devolvió una imagen".into()),
        };
        let bytes = response.bytes().await.map_err(|e| e.to_string())?;
        return Ok(serde_json::json!({"image": format!("data:{mime};base64,{}", STANDARD.encode(bytes))}));
    }
    let value: Value = response.json().await.map_err(|_| "El Hub devolvió una respuesta no válida".to_string())?;
    if !status.is_success() { return Err(value.get("error").and_then(Value::as_str).unwrap_or("La operación falló").into()); }
    Ok(value)
}
#[tauri::command]
async fn open_hub(space_id: Option<String>, decision: Option<String>) -> Result<(), String> {
    let base = std::env::var("AGORA_HUB_URL").unwrap_or_else(|_| "http://127.0.0.1:8810".into());
    let mut parsed = reqwest::Url::parse(&base).map_err(|_| "Dirección del Hub no válida")?;
    if parsed.scheme() != "http" || !matches!(parsed.host_str(), Some("127.0.0.1") | Some("localhost") | Some("[::1]")) || parsed.username() != "" || parsed.password().is_some() || parsed.path() != "/" || parsed.query().is_some() || parsed.fragment().is_some() {
        return Err("El Hub debe estar en una dirección local".into());
    }
    if let Some(id) = space_id { parsed.query_pairs_mut().append_pair("space_id", &id); }
    let view = if let Some(key) = decision {
        if !decision_key(&key) { return Err("Enlace a decisión no válido".into()); }
        parsed.query_pairs_mut().append_pair("decision", &key);
        "hub/orders"
    } else {
        "agora"
    };
    let ticket = launch_ticket(&hub_client()?, base.trim_end_matches('/')).await?;
    parsed.set_fragment(Some(&format!("hub-session={ticket}&view={view}")));
    std::process::Command::new("explorer.exe").arg(parsed.as_str()).spawn().map_err(|_| "No se puede abrir Hoard Hub".to_string())?;
    Ok(())
}
fn main() { tauri::Builder::default().invoke_handler(tauri::generate_handler![hub_request,open_hub]).run(tauri::generate_context!()).expect("Could not start the coordination window"); }

#[cfg(test)]
mod tests {
    use super::{decision_key, piece_preview, valid_agora_path};
    #[test]
    fn relay_stays_inside_coordination_routes() {
        assert!(valid_agora_path("/api/agora/threads/32/recent?limit=60"));
        for path in ["https://example.com/api/agora/", "/api/agora/../agent/call", "/api/agora/%2e%2e/agent/call", "/api/agora/%2f../tokens", "/api/agora/board#fragment", "/api/agora/..\\tokens"] {
            assert!(!valid_agora_path(path), "unexpected route: {path}");
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
