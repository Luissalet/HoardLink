#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use serde_json::Value;

fn valid_agora_path(path: &str) -> bool {
    let route = path.split('?').next().unwrap_or("");
    route.starts_with("/api/agora/") && !path.contains('#') &&
        route.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'/' | b'-' | b'_'))
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
    let client = reqwest::Client::builder().timeout(std::time::Duration::from_secs(30)).redirect(reqwest::redirect::Policy::none()).build().map_err(|e|e.to_string())?;
    let mut request = if let Some(ref value) = body { client.post(format!("{}{}",base.trim_end_matches('/'),path)).json(value) } else { client.get(format!("{}{}",base.trim_end_matches('/'),path)) };
    request = request.header("Origin",base.trim_end_matches('/')).header("Sec-Fetch-Site","same-origin");
    let response = request.send().await.map_err(|_| "No se puede conectar con el Hub local".to_string())?;
    let status = response.status();
    let value: Value = response.json().await.map_err(|_| "El Hub devolvió una respuesta no válida".to_string())?;
    if !status.is_success() { return Err(value.get("error").and_then(Value::as_str).unwrap_or("La operación falló").into()); }
    Ok(value)
}
#[tauri::command]
fn open_hub(space_id: Option<String>) -> Result<(), String> {
    let base = std::env::var("AGORA_HUB_URL").unwrap_or_else(|_| "http://127.0.0.1:8810".into());
    let mut parsed = reqwest::Url::parse(&base).map_err(|_| "Dirección del Hub no válida")?;
    if parsed.scheme() != "http" || !matches!(parsed.host_str(), Some("127.0.0.1") | Some("localhost") | Some("[::1]")) || parsed.username() != "" || parsed.password().is_some() || parsed.path() != "/" || parsed.query().is_some() || parsed.fragment().is_some() {
        return Err("El Hub debe estar en una dirección local".into());
    }
    if let Some(id) = space_id { parsed.query_pairs_mut().append_pair("space_id", &id); }
    parsed.set_fragment(Some("agora"));
    std::process::Command::new("explorer.exe").arg(parsed.as_str()).spawn().map_err(|_| "No se puede abrir Hoard Hub".to_string())?;
    Ok(())
}
fn main() { tauri::Builder::default().invoke_handler(tauri::generate_handler![hub_request,open_hub]).run(tauri::generate_context!()).expect("Could not start the coordination window"); }

#[cfg(test)]
mod tests {
    use super::valid_agora_path;
    #[test]
    fn relay_stays_inside_coordination_routes() {
        assert!(valid_agora_path("/api/agora/threads/32/recent?limit=60"));
        for path in ["https://example.com/api/agora/", "/api/agora/../agent/call", "/api/agora/%2e%2e/agent/call", "/api/agora/%2f../tokens", "/api/agora/board#fragment", "/api/agora/..\\tokens"] {
            assert!(!valid_agora_path(path), "unexpected route: {path}");
        }
    }
}
