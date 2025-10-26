-- @description Open Project via xdg-desktop-portal (per-action last dir + last choices)
-- @about
--   Opens the native (GNOME/KDE) file chooser via xdg-desktop-portal using a Python helper.
--   - Rejects Document-Portal paths (/run/user/<uid>/doc/...) with a user-facing error
--   - No output to REAPER's console
--   - Remembers the last directory and checkbox choices PER ACTION
--   - Stores state in: REAPER/Data/PortalFileChooser/<Action>.state  (simple key=value)
--   - Reads portal choices from the helper JSON output
--   - Implements: "Open in new project tab" and "Open with FX offline (recovery mode)"
--   - Choices are only saved when a file is actually opened (not on cancel)

----------------------------------------
-- Aliases
----------------------------------------
local reaper = reaper

----------------------------------------
-- Path & file helpers
----------------------------------------

--- Return the current script's directory and filename.
local function script_dir_and_name()
  local _, p = reaper.get_action_context()
  local dir  = p:match("^(.*)[/\\]") or "."
  local name = p:match("([^/\\]+)$") or "portal_open_project.lua"
  return dir, name
end

--- Join two paths with the platform-specific separator.
local function join(a, b)
  local sep = package.config:sub(1,1)
  if a:sub(-1) == sep then return a .. b else return a .. sep .. b end
end

--- Test if a file exists (readable).
local function exists(path)
  local f = io.open(path, "r")
  if f then f:close(); return true end
  return false
end

--- Read a whole file as text and trim trailing whitespace; return nil if unreadable/empty.
local function read_text(path)
  local f = io.open(path, "r")
  if not f then return nil end
  local s = f:read("*a") or ""
  f:close()
  s = s:gsub("[%s\r\n]+$", "")
  if s == "" then return nil end
  return s
end

--- Write text plus newline to file (overwrite). Returns true on success.
local function write_text(path, content)
  local f = io.open(path, "w")
  if not f then return false end
  f:write(content or "")
  f:write("\n")
  f:close()
  return true
end

--- Quote a string for shell usage.
local function q(s) return string.format("%q", s) end

--- Run a shell command and capture stdout as a string (empty string on no output).
local function run_capture_stdout(cmd)
  local p = io.popen(cmd, "r")
  if not p then return nil end
  local out = p:read("*a") or ""
  p:close()
  return out
end

----------------------------------------
-- Minimal key=value state file
----------------------------------------

--- Load state from a simple key=value file.
--- Legacy compatibility: if the file contains only a bare path (no '='), treat it as dir.
local function load_state(path)
  local s = {}
  local f = io.open(path, "r")
  if not f then return s end
  local content = f:read("*a") or ""
  f:close()

  if not content:find("=") then
    local dir = content:gsub("[%s\r\n]+$", "")
    if dir ~= "" then s.dir = dir end
    return s
  end

  for line in content:gmatch("[^\r\n]+") do
    local k, v = line:match("^%s*([^=%s]+)%s*=%s*(.-)%s*$")
    if k then s[k] = v end
  end
  return s
end

--- Save state as key=value (dir + two booleans as 0/1).
local function save_state(path, state)
  local f = io.open(path, "w")
  if not f then return false end
  if state.dir then f:write("dir=", state.dir, "\n") end
  f:write("open_in_new_tab=", (state.open_in_new_tab and "1" or "0"), "\n")
  f:write("fx_offline=",      (state.fx_offline      and "1" or "0"), "\n")
  f:close()
  return true
end

--- Convert various truthy strings (1/true/yes/y/on) to boolean.
local function truthy01(x)
  if type(x) == "boolean" then return x end
  if type(x) ~= "string" then return false end
  x = x:lower()
  return (x == "1" or x == "true" or x == "yes" or x == "y" or x == "on")
end

----------------------------------------
-- Minimal JSON parsing helpers (single-level keys)
----------------------------------------

--- Extract a top-level JSON string value by key (unescapes \" and \\).
local function json_get_string(json, key)
  local pat = '"'..key..'":%s*"(.-)"'
  local v = json:match(pat)
  if v then v = v:gsub('\\"','"'):gsub("\\\\","\\") end
  return v
end

--- Return true if the JSON has `"key": null` at top level.
local function json_has_null(json, key)
  local pat = '"'..key..'":%s*null'
  return json:match(pat) ~= nil
end

--- Extract a boolean from the nested "choices" object by choice key.
local function json_get_choice_bool(json, choice_key)
  local block = json:match([["choices"%s*:%s*{(.-)}]])
  if not block then return false end
  local pat_true  = '"'..choice_key..'":%s*true'
  local pat_false = '"'..choice_key..'":%s*false'
  if block:match(pat_true) then return true end
  if block:match(pat_false) then return false end
  return false
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
-- Per-action state file (under REAPER/Data/PortalFileChooser)
----------------------------------------

local RES = reaper.GetResourcePath()
local CFG_DIR = join(join(RES, "Data"), "PortalFileChooser")

-- Create directory if needed (use REAPER helper if available)
if reaper.RecursiveCreateDirectory then
  reaper.RecursiveCreateDirectory(CFG_DIR, 0)
else
  os.execute(string.format('mkdir -p %s', q(CFG_DIR)))
end

-- Derive a safe base name from script file name
local base = (SCRIPT_NAME:gsub("%.lua$", "")):gsub("[^%w%._%-]+", "_")
local STATE_FILE = join(CFG_DIR, base .. ".state")

-- Load persisted state (dir + choices)
local state   = load_state(STATE_FILE)
local lastdir = state.dir
local def_open_in_new_tab = truthy01(state.open_in_new_tab or "0")
local def_fx_offline      = truthy01(state.fx_offline      or "0")

----------------------------------------
-- Define the portal dialog (arguments for Python helper)
----------------------------------------

local args = {
  "--out", "-",
  "--title", "Open project",
-- Accept label should only replaced if necessary, as it destroys the localization that KDE brings by default. The localization would have to be provided manually by REAPER.
--  "--accept-label", "_Open",

  -- File filters
  "--filter", "All Supported Projects|*.RPP;*.TXT;*.EDL;PROJ*.TXT;*.ADL;clipsort.log;*.RPP-BAK",
  "--filter", "REAPER Project files (*.RPP)|*.RPP",
  "--filter", "EDL TXT (Vegas) files (*.TXT)|*.TXT",
  "--filter", "EDL (Samplitude) files (*.EDL)|*.EDL",
  "--filter", "RADAR Session TXT files (PROJ*.TXT)|PROJ*.TXT",
  "--filter", "AES-31 files (*.ADL)|*.ADL",
  "--filter", "NINJAM log files (clipsort.log)|clipsort.log",
  "--filter", "REAPER Project Backup files (*.RPP-BAK)|*.RPP-BAK",
  "--filter", "All files (*.*)|*.*",
  "--initial-filter", "All Supported Projects",

  -- Choices with defaults from persisted state
  "--choice", ("open_in_new_tab|Open in new project tab|%s"):format(def_open_in_new_tab and "true" or "false"),
  "--choice", ("fx_offline|Open with FX offline (recovery mode)|%s"):format(def_fx_offline and "true" or "false"),
}

-- Preferred start directory (helper will fall back to $HOME if invalid)
if lastdir and lastdir ~= "" then
  table.insert(args, "--current-folder")
  table.insert(args, lastdir)
end

----------------------------------------
-- Execute helper and capture JSON
----------------------------------------

local cmd = q(PY) .. " -u " .. q(HELPER)
for i = 1, #args do
  cmd = cmd .. " " .. q(args[i])
end

local out = run_capture_stdout(cmd)
if not out or out == "" then
  -- No output (e.g., helper failed or was canceled very early) → just exit quietly.
  return
end

----------------------------------------
-- Parse result (path + choices)
----------------------------------------

local path = json_get_string(out, "path")
if not path and not json_has_null(out, "path") then
  -- Unexpected JSON shape → abort without side effects.
  return
end

----------------------------------------
-- Reject Document-Portal paths (treat like cancel)
----------------------------------------

local function is_doc_portal_path(p)
  -- Matches /run/user/<uid>/doc/...
  return type(p) == "string" and p:match("^/run/user/%d+/doc/") ~= nil
end

if path and is_doc_portal_path(path) then
  reaper.MB(
    "The selected path is outside Flatpak's allowed filesystem and was provided via the Documents portal.\n\n" ..
    "Please choose a location that your REAPER Flatpak can access directly (e.g. inside your Home folder or a path you granted in Flatseal).",
    "Portal", 0
  )
  -- Treat like cancel: do nothing further (no state updates, no project open).
  return
end

----------------------------------------
-- Utility: set all FX offline (recovery mode)
----------------------------------------

local function set_all_fx_offline_for_project(proj)
  -- Master FX
  local master = reaper.GetMasterTrack(proj)
  if master then
    local mfx = reaper.TrackFX_GetCount(master)
    for i = 0, mfx-1 do
      reaper.TrackFX_SetOffline(master, i, true)
    end
  end

  -- Track FX
  local trackCount = reaper.CountTracks(proj)
  for ti = 0, trackCount-1 do
    local tr = reaper.GetTrack(proj, ti)
    local fxCount = reaper.TrackFX_GetCount(tr)
    for fi = 0, fxCount-1 do
      reaper.TrackFX_SetOffline(tr, fi, true)
    end
  end

  -- Take FX
  local itemCount = reaper.CountMediaItems(proj)
  for ii = 0, itemCount-1 do
    local item = reaper.GetMediaItem(proj, ii)
    local takeCount = reaper.GetMediaItemNumTakes(item)
    for tk = 0, takeCount-1 do
      local take = reaper.GetMediaItemTake(item, tk)
      if take then
        local tfxCount = reaper.TakeFX_GetCount(take)
        for tfi = 0, tfxCount-1 do
          reaper.TakeFX_SetOffline(take, tfi, true)
        end
      end
    end
  end
end

----------------------------------------
-- Apply result & persist state (only when a file was chosen)
----------------------------------------

if path and path ~= "" then
  -- Update last dir
  local dir = path:match("^(.*)[/\\]")
  if dir and dir ~= "" then
    state.dir = dir
  end

  -- Read choices from JSON (choices object)
  local open_in_new_tab = json_get_choice_bool(out, "open_in_new_tab")
  local fx_offline      = json_get_choice_bool(out, "fx_offline")

  -- Persist choices (only on successful selection)
  state.open_in_new_tab = open_in_new_tab
  state.fx_offline      = fx_offline
  save_state(STATE_FILE, state)

  -- Execute action
  if open_in_new_tab then
    -- Default command ID for "New project tab"
    reaper.Main_OnCommand(40859, 0)
  end

  reaper.PreventUIRefresh(1)
  reaper.Main_openProject(path)
  if fx_offline then
    reaper.Undo_BeginBlock()
    set_all_fx_offline_for_project(0)
    reaper.Undo_EndBlock("Set all FX offline (recovery)", -1)
  end
  reaper.PreventUIRefresh(-1)
end
