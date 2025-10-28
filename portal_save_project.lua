-- @description Save Project via xdg-desktop-portal (per-action last dir + create-subdir + force UPPERCASE extension)
-- @about
--   Saves the current project using the native (GNOME/KDE) file chooser via xdg-desktop-portal and a Python helper.
--   - Uses the helper in Save mode (--save)
--   - Start hint priority: current-file (if project already has a path) > lastdir > none (helper falls back)
--   - Persists last directory PER ACTION in: REAPER/Data/PortalFileChooser/<Action>.state (key=value)
--   - Checkbox: "Create subdirectory for project" (persisted)
--   - Appends correct extension (.RPP/.TXT/.EDL) in UPPERCASE if missing (based on selected filter; otherwise heuristics)
--   - Rejects /run/user/<uid>/doc/... (Documents portal) with a user-facing error (treated as cancel)
--   - Ensures the just-saved project becomes the active one

----------------------------------------
-- Aliases
----------------------------------------
local reaper = reaper

----------------------------------------
-- Path & file helpers
----------------------------------------

local function script_dir_and_name()
  local _, p = reaper.get_action_context()
  local dir  = p:match("^(.*)[/\\]") or "."
  local name = p:match("([^/\\]+)$") or "portal_save_project.lua"
  return dir, name
end

local function join(a, b)
  local sep = package.config:sub(1,1)
  if a:sub(-1) == sep then return a .. b else return a .. sep .. b end
end

local function exists(path)
  local f = io.open(path, "r")
  if f then f:close(); return true end
  return false
end

local function write_text(path, content)
  local f = io.open(path, "w")
  if not f then return false end
  f:write(content or "")
  f:write("\n")
  f:close()
  return true
end

local function q(s) return string.format("%q", s) end

local function run_capture_stdout(cmd)
  local p = io.popen(cmd, "r")
  if not p then return nil end
  local out = p:read("*a") or ""
  p:close()
  return out
end

----------------------------------------
-- Minimal key=value state
----------------------------------------

local function load_state(path)
  local s = {}
  local f = io.open(path, "r")
  if not f then return s end
  for line in f:lines() do
    local k, v = line:match("^%s*([^=%s]+)%s*=%s*(.-)%s*$")
    if k then s[k] = v end
  end
  f:close()
  return s
end

local function save_state(path, state)
  local f = io.open(path, "w")
  if not f then return false end
  if state.dir then f:write("dir=", state.dir, "\n") end
  f:write("create_subdir=", (state.create_subdir and "1" or "0"), "\n")
  f:close()
  return true
end

local function truthy01(x)
  if type(x) == "boolean" then return x end
  if type(x) ~= "string" then return false end
  x = x:lower()
  return (x == "1" or x == "true" or x == "yes" or x == "y" or x == "on")
end

----------------------------------------
-- Tiny JSON helpers (pattern-based)
----------------------------------------

local function json_get_string(json, key)
  local pat = '"'..key..'":%s*"(.-)"'
  local v = json:match(pat)
  if v then v = v:gsub('\\"','"'):gsub("\\\\","\\") end
  return v
end

local function json_has_null(json, key)
  local pat = '"'..key..'":%s*null'
  return json:match(pat) ~= nil
end

local function json_get_choice_bool(json, choice_key)
  local block = json:match([["choices"%s*:%s*{(.-)}]])
  if not block then return false end
  local pat_true  = '"'..choice_key..'":%s*true'
  local pat_false = '"'..choice_key..'":%s*false'
  if block:match(pat_true) then return true end
  if block:match(pat_false) then return false end
  return false
end

-- parse ["*.RPP","*.TXT"] into table
local function json_get_string_array(json, key)
  local arr = {}
  local block = json:match('"'..key..'":%s*%[(.-)%]')
  if not block then return nil end
  for s in block:gmatch('"(.-)"') do
    s = s:gsub('\\"','"'):gsub("\\\\","\\")
    table.insert(arr, s)
  end
  return arr
end

----------------------------------------
-- Locate Python helper
----------------------------------------

local PY = "python3"
local SCRIPT_DIR, SCRIPT_NAME = script_dir_and_name()
local HELPER = join(SCRIPT_DIR, "reaper_portal_fc.py")

if not exists(HELPER) then
  local res = reaper.GetResourcePath()
  local candidates = {
    join(res, "Scripts/reaper_portal_fc.py"),
    join(res, "Scripts/ReaperPortalFileChooser/reaper_portal_fc.py"),
  }
  for _, p in ipairs(candidates) do
    if exists(p) then HELPER = p; break end
  end
end

if not exists(HELPER) then
  reaper.MB(
    "Portal helper (reaper_portal_fc.py) not found.\n" ..
    "Place it next to this script or in REAPER/Scripts.",
    "Portal", 0
  )
  return
end

----------------------------------------
-- Per-action state (REAPER/Data/PortalFileChooser)
----------------------------------------

local RES = reaper.GetResourcePath()
local CFG_DIR = join(join(RES, "Data"), "PortalFileChooser")
if reaper.RecursiveCreateDirectory then
  reaper.RecursiveCreateDirectory(CFG_DIR, 0)
else
  os.execute(string.format('mkdir -p %s', q(CFG_DIR)))
end

local base = (SCRIPT_NAME:gsub("%.lua$", "")):gsub("[^%w%._%-]+", "_")
local STATE_FILE = join(CFG_DIR, base .. ".state")

local state   = load_state(STATE_FILE)
local lastdir = state.dir
local def_create_subdir = truthy01(state.create_subdir or "0")

----------------------------------------
-- Determine start hint: current-file > lastdir > none
----------------------------------------

local function current_project_filename()
  local _, fn = reaper.EnumProjects(-1, "")
  if fn and fn ~= "" then return fn end
  return nil
end

local current_file = current_project_filename()

----------------------------------------
-- Build Save dialog args
----------------------------------------

local args = {
  "--out", "-",
  "--save",
  "--title", "Save project",

  -- Filters (order matters for default)
  "--filter", "REAPER Project files (*.RPP)|*.RPP",
  "--filter", "EDL TXT (Vegas) files (*.TXT)|*.TXT",
  "--filter", "EDL (Samplitude) files (*.EDL)|*.EDL",
  "--filter", "All files (*.*)|*.*",
  "--initial-filter", "REAPER Project files (*.RPP)",

  -- Persisted checkbox
  "--choice", ("create_subdir|Create subdirectory for project|%s"):format(def_create_subdir and "true" or "false"),
}

-- Start hint priority
if current_file then
  table.insert(args, "--current-file");   table.insert(args, current_file)
elseif lastdir and lastdir ~= "" then
  table.insert(args, "--current-folder"); table.insert(args, lastdir)
end

----------------------------------------
-- Execute helper
----------------------------------------

local function is_doc_portal_path(p)
  return type(p) == "string" and p:match("^/run/user/%d+/doc/") ~= nil
end

local cmd = q(PY) .. " -u " .. q(HELPER)
for i = 1, #args do cmd = cmd .. " " .. q(args[i]) end
local out = run_capture_stdout(cmd)
if not out or out == "" then return end

----------------------------------------
-- Parse result
----------------------------------------

local path = json_get_string(out, "path")
if not path and not json_has_null(out, "path") then return end
if path and is_doc_portal_path(path) then
  reaper.MB(
    "The selected path is outside Flatpak's allowed filesystem and was provided via the Documents portal.\n\n" ..
    "Please choose a location that your REAPER Flatpak can access directly (e.g. inside your Home folder or a path you granted in Flatseal).",
    "Portal", 0
  )
  return
end

-- Filter info from helper (if backend provided it)
local selected_filter_label = json_get_string(out, "selected_filter_label")
local selected_filter_globs = json_get_string_array(out, "selected_filter_globs")

----------------------------------------
-- Utilities
----------------------------------------

local function dirname(p)  return p:match("^(.*)[/\\]") or "" end
local function basename(p) return p:match("([^/\\]+)$") or p end
local function strip_ext(name) return (name:gsub("%.[^%.\\/:]+$", "")) end

-- Append extension ONLY if missing; when we append, force UPPERCASE (REAPER UI parity)
local function ensure_ext_upper(file_path, wanted_ext)
  -- wanted_ext like ".rpp" or ".RPP"
  if not wanted_ext or wanted_ext == "" then return file_path end
  local wanted = wanted_ext:sub(1,1) == "." and wanted_ext or ("." .. wanted_ext)
  local wanted_uc = wanted:upper()
  local lower = file_path:lower()
  if lower:sub(-#wanted) ~= wanted:lower() then
    return file_path .. wanted_uc
  end
  return file_path
end

local function guess_ext_from_filter(label, globs)
  -- Prefer filter glob if available, else from label keywords.
  local function ext_from_globs(gs)
    if not gs then return nil end
    for _, g in ipairs(gs) do
      local ext = g:match("%*%.([A-Za-z0-9]+)$")
      if ext then return "." .. ext:lower() end
    end
    return nil
  end
  local e = ext_from_globs(globs)
  if e then return e end
  label = (label or ""):lower()
  if label:find("samplitude") or label:find("%.edl") then return ".edl" end
  if label:find("vegas") or label:find("%.txt") then return ".txt" end
  if label:find("reaper") or label:find("%.rpp") then return ".rpp" end
  return nil
end

local function mkdir_p(dir)
  if dir == "" then return true end
  if reaper.RecursiveCreateDirectory then
    reaper.RecursiveCreateDirectory(dir, 0)
  else
    os.execute(string.format('mkdir -p %s', q(dir)))
  end
  return true
end

----------------------------------------
-- Apply save (only when a path was chosen)
----------------------------------------

if path and path ~= "" then
  -- Determine desired extension from portal-selected filter (KDE) or fallback heuristics (GTK)
  local ext = guess_ext_from_filter(selected_filter_label, selected_filter_globs)
  if not ext then
    -- Heuristic fallback: infer from typed name; else default to .rpp
    local lower = path:lower()
    if     lower:match("%.rpp$") then ext = ".rpp"
    elseif lower:match("%.txt$") then ext = ".txt"
    elseif lower:match("%.edl$") then ext = ".edl"
    else ext = ".rpp" end
  end

  -- Append extension if missing; when appending, use UPPERCASE (.RPP/.TXT/.EDL)
  path = ensure_ext_upper(path, ext)

  -- Read checkbox
  local want_subdir = json_get_choice_bool(out, "create_subdir")

  -- Build final path with optional subdirectory
  local final_path = path
  if want_subdir then
    local parent  = dirname(path)
    local file    = basename(path)
    local name_wo = strip_ext(file); if name_wo == "" then name_wo = "Project" end
    local target_dir = join(parent, name_wo)
    mkdir_p(target_dir)
    final_path = join(target_dir, file)
  end

  -- Save and then explicitly open the saved project (guarantee it's active)
  reaper.PreventUIRefresh(1)
  local proj = 0
  reaper.Main_SaveProjectEx(proj, final_path, 0) -- 0 = not a copy
  reaper.Main_openProject(final_path)
  reaper.PreventUIRefresh(-1)

  -- Persist last dir & checkbox
  local saved_dir = dirname(final_path)
  if saved_dir ~= "" then state.dir = saved_dir end
  state.create_subdir = want_subdir
  save_state(STATE_FILE, state)
end
