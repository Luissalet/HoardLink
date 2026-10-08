// Atlas shared live files: use the returned filesystem paths directly.
// Configure ../hoard-link.js with this app's own token. No cloud or hidden copies.
import { callTool } from "./fam-services.js";

export async function workspaceCall(tool, arguments_ = {}, {timeoutS = 120} = {}) {
  const response = await callTool("atlas", `atlas_${tool}`, arguments_, {timeoutS});
  return response.ok ? {...response.data, via: "atlas"} : response;
}
export const workspaceProjects = (sphere) => workspaceCall("projects", sphere ? {sphere} : {});
export const workspaceProject = (projectId) => workspaceCall("project", {project_id: projectId});
export const workspaceCreate = (name, {members, owner, sphere = "personal", goal = "", requestId} = {}) =>
  workspaceCall("project_create", {name, members, sphere, goal, ...(owner ? {owner} : {}), ...(requestId ? {request_id: requestId} : {})});
export const workspaceLocation = (projectId, {area = "shared", app} = {}) =>
  workspaceCall("location", {project_id: projectId, area, ...(app ? {app} : {})});
export const workspaceRegister = (projectId, relativePath, {title = "", requestId} = {}) =>
  workspaceCall("file_register", {project_id: projectId, relative_path: relativePath, title, ...(requestId ? {request_id: requestId} : {})});
export const workspaceResolve = (fileId) => workspaceCall("file_resolve", {file_id: fileId});
export const workspaceLookup = (projectId, sourceIds, recipe) =>
  workspaceCall("derived_lookup", {project_id: projectId, source_ids: sourceIds, recipe});
export const workspacePublish = (projectId, sourceIds, recipe, outputId, {sourceRevisions, requestId} = {}) =>
  workspaceCall("derived_publish", {project_id: projectId, source_ids: sourceIds, recipe, output_id: outputId,
    source_revisions: sourceRevisions, ...(requestId ? {request_id: requestId} : {})});
export const workspaceContext = (projectId, fileIds = []) =>
  workspaceCall("context", {project_id: projectId, file_ids: fileIds});
