-- @description Open Project via native portal (Lua -> Python writes JSON file) (portable + cancel-friendly)
-- @version 0.3.0

local reaper = reaper

-- ---------- Pfad-Ermittlung: Helper neben diesem Lua-Skript (portabel), mit Fallback ----------
local function script_dir()
  local _, thisfn, _, _, _, _ = reaper.get_action_context()
  return thisfn:match("^(.*)[/\\]") or "."
end

local function file_exists(p)
  local f = io.open(p, "r"); if f then f:close(); return true end; return false
end

local function join(a,b)
  if a:sub(-1) == "/" or a:sub(-1) == "\\" then return a..b end
  local sep = package.config:sub(1,1)
  return a..sep..b
end

local SCRIPT_DIR = script_dir()
local HELPER_REL = "reaper_portal_open.py"              -- liegt idealerweise neben diesem Lua
local HELPER = join(SCRIPT_DIR, HELPER_REL)

if not file_exists(HELPER) then
  -- Fallback: im REAPER-ResourcePath /Scripts/<Projektordner>/...
  local res = reaper.GetResourcePath()
  local candidates = {
    join(res, join("Scripts","ReaperPortalFileChooser/"..HELPER_REL)),
    join(res, HELPER_REL),
  }
  for _,p in ipairs(candidates) do
    if file_exists(p) then HELPER = p; break end
  end
end

-- Python (ggf. anpassen, falls anders installiert)
local PY = "python3"

-- ---------- Utils ----------
local function tmpfile(ext)
  local p = os.tmpname():gsub("\\","/")
  if ext then p = p .. ext end
  return p
end

local function readfile(p)
  local f = io.open(p, "r"); if not f then return nil end
  local s = f:read("*a"); f:close(); return s
end

-- Minimaler JSON-Parser für unser Format
local function json_unescape(s)
  s = s:gsub('\\"','"'):gsub("\\\\","\\")
  s = s:gsub("\\n","\n"):gsub("\\t","\t"):gsub("\\r","\r")
  return s
end

local function parse_portal_json(txt)
  if not txt or txt == "" then return nil end
  local err = txt:match('"error"%s*:%s*"(.-)"'); if err then err = json_unescape(err) end
  local path = txt:match('"path"%s*:%s*"(.-)"'); if path then path = json_unescape(path) end
  local newtab = txt:match('"open_in_new_tab"%s*:%s*(true)') and true or false
  local fxoff  = txt:match('"fx_offline"%s*:%s*(true)') and true or false
  return { error = err, path = path, choices = { open_in_new_tab = newtab, fx_offline = fxoff } }
end

-- ---------- Hauptlogik ----------
local function main()
  -- Helper da?
  if not file_exists(HELPER) then
    reaper.ShowConsoleMsg(("Portal-Helper nicht gefunden.\nErwartet: %s\n"):format(HELPER))
    return
  end

  local out_path = tmpfile(".reaper_portal.json")
  local err_path = tmpfile(".reaper_portal.err")
  os.remove(out_path); os.remove(err_path)

  -- Python starten – Helper schreibt direkt nach --out/--err
  local cmd = string.format('"%s" -u "%s" --out "%s" --err "%s"', PY, HELPER, out_path, err_path)
  local ret, _ = reaper.ExecProcess(cmd, 120000)  -- bis zu 120 s warten

  -- Datei einlesen (kann beim echten Abbrechen fehlen)
  local json_text = readfile(out_path)
  local err_text  = readfile(err_path) or ""

  -- FALL 1: Keine JSON-Datei + kein Fehlertext ⇒ als "Abgebrochen" behandeln, freundlich & still
  if (not json_text or json_text == "") and (err_text == "" or err_text == nil) then
    -- Optional: leise return; oder kleine Statuszeile:
    -- reaper.ShowConsoleMsg("Abgebrochen.\n")
    return
  end

  -- FALL 2: Keine JSON-Datei, aber Fehlertext vorhanden ⇒ echter Fehler
  if not json_text or json_text == "" then
    reaper.ShowConsoleMsg(
      ("Portal-Helper: keine Ausgabe-Datei.\nExit=%s\nCMD: %s\nERR:\n%s\n")
        :format(tostring(ret), cmd, err_text)
    )
    return
  end

  local obj = parse_portal_json(json_text)
  if not obj then
    reaper.ShowConsoleMsg(
      ("Portal-Helper: JSON konnte nicht gelesen werden.\nExit=%s\nCMD: %s\nJSON:\n%s\nERR:\n%s\n")
        :format(tostring(ret), cmd, json_text, err_text)
    )
    return
  end

  if obj.error then
    -- Falls der Helper im Fehlerfall JSON mit "error" geschrieben hat
    reaper.ShowConsoleMsg(("Portal-Helper-Fehler: %s\n"):format(tostring(obj.error)))
    return
  end

  -- Normalfall
  if not obj.path or obj.path == "" then
    -- JSON vorhanden, aber ohne Pfad ⇒ Abgebrochen im Dialog
    -- (leise)
    return
  end

  reaper.ShowConsoleMsg(
    string.format("Ausgewählt: %s\nopen_in_new_tab=%s, fx_offline=%s\n",
      obj.path, tostring(obj.choices.open_in_new_tab), tostring(obj.choices.fx_offline))
  )

  -- Projekt öffnen
  reaper.Main_openProject(obj.path)
end

main()
