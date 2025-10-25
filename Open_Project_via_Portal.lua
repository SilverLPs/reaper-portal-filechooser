-- @description Open Project via xdg-desktop-portal (argument-driven helper)
-- @version 1.0.0
-- @about
--   Öffnet den nativen File-Chooser (GNOME/KDE) via Portal.
--   - Keine Ausgabe in die REAPER-Konsole
--   - Merkt den letzten Ordner (ExtState)
--   - Liest Optionen/Checkboxen aus der JSON-Ausgabe des Python-Helpers
--   Voraussetzung: reaper_portal_fc.py liegt neben diesem Skript oder in REAPER/Scripts.

local reaper = reaper

-- ---------- kleine Utils ----------
local function script_dir()
  local _, p = reaper.get_action_context()
  return p:match("^(.*)[/\\]") or "."
end

local function join(a, b)
  local sep = package.config:sub(1,1)
  if a:sub(-1) == sep then return a .. b else return a .. sep .. b end
end

local function exists(p)
  local f = io.open(p, "r")
  if f then f:close(); return true end
  return false
end

local function q(s) return string.format("%q", s) end

local function run_capture_stdout(cmd)
  local p = io.popen(cmd, "r")
  if not p then return nil end
  local out = p:read("*a") or ""
  p:close()
  return out
end

-- ---------- Python-Helper finden ----------
local PY = "python3"
local HELPER = join(script_dir(), "reaper_portal_fc.py")
if not exists(HELPER) then
  local res = reaper.GetResourcePath()
  local candidates = {
    join(res, "Scripts/reaper_portal_fc.py"),
    join(res, "Scripts/ReaperPortalFileChooser/reaper_portal_fc.py"),
  }
  for _, p in ipairs(candidates) do if exists(p) then HELPER = p; break end end
end
if not exists(HELPER) then
  reaper.MB("Portal-Helper (reaper_portal_fc.py) nicht gefunden.\n" ..
            "Lege ihn neben dieses Skript oder in REAPER/Scripts.", "Portal", 0)
  return
end

-- ---------- Persistenter Startordner ----------
local EXTNS   = "portal_fc"
local EXTKEY  = "open_last_dir"
local lastdir = reaper.GetExtState(EXTNS, EXTKEY)
if lastdir == "" then lastdir = nil end

-- ---------- Dialog-spezifische Definition (hier schlank anpassbar) ----------
local args = {
  "--out", "-",
  "--title", "Open project (Portal)",
  "--accept-label", "_Open",
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
  "--choice", "open_in_new_tab|Open in new project tab|false",
  "--choice", "fx_offline|Open with FX offline (recovery mode)|false",
  -- kein --modal: standardmäßig False (kein Abdunkeln/Sperren)
}
if lastdir then
  table.insert(args, "--current-folder"); table.insert(args, lastdir)
end

-- ---------- Kommando bauen und ausführen ----------
local cmd = q(PY) .. " -u " .. q(HELPER)
for i = 1, #args do cmd = cmd .. " " .. q(args[i]) end

local out = run_capture_stdout(cmd)
if not out or out == "" then return end

-- ---------- minimale JSON-Auswertung (zielgerichtet) ----------
-- Wir lesen nur "path" (String oder null) und zwei Choices (Booleans).
local function json_get_string(s, key)
  -- findet "key": "value" (ohne verschachtelte Ebenen)
  local pat = '"'..key..'":%s*"(.-)"'
  local v = s:match(pat)
  if v then
    v = v:gsub('\\"','"'):gsub("\\\\","\\")
  end
  return v
end

local function json_has_null(s, key)
  local pat = '"'..key..'":%s*null'
  return s:match(pat) ~= nil
end

local function json_get_bool(s, key)
  local pat_true  = '"'..key..'":%s*true'
  local pat_false = '"'..key..'":%s*false'
  if s:match(pat_true) then return true
  elseif s:match(pat_false) then return false
  else return false end
end

local path = json_get_string(out, "path")
if not path and not json_has_null(out, "path") then
  -- Unerwartetes Format -> sicher beenden
  return
end

-- ---------- bei Auswahl: Pfad merken, Optionen anwenden, Projekt öffnen ----------
if path and path ~= "" then
  -- Ordner merken
  local dir = path:match("^(.*)[/\\]")
  if dir and dir ~= "" then
    reaper.SetExtState(EXTNS, EXTKEY, dir, true)
  end

  -- Choices auswerten
  local open_in_new_tab = json_get_bool(out, "open_in_new_tab")
  local fx_offline      = json_get_bool(out, "fx_offline")

  -- Optional: neuen Projekt-Tab öffnen (Action-ID ggf. prüfen/anpassen)
  if open_in_new_tab then
    -- Standardmäßig ist "New project tab" die Command-ID 40859
    reaper.Main_OnCommand(40859, 0)
  end

  -- Projekt laden
  reaper.Main_openProject(path)

  -- Optional: alle FX offline (Recovery)
  if fx_offline then
    local proj = 0
    reaper.Undo_BeginBlock()
    -- Track-FX
    local trackCount = reaper.CountTracks(proj)
    for ti = 0, trackCount-1 do
      local tr = reaper.GetTrack(proj, ti)
      local fxCount = reaper.TrackFX_GetCount(tr)
      for fi = 0, fxCount-1 do
        reaper.TrackFX_SetOffline(tr, fi, true)
      end
    end
    -- Take-FX
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
    reaper.Undo_EndBlock("Set all FX offline (recovery)", -1)
  end
end
