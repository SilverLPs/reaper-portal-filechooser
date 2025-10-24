-- @description Open Project via native portal (stdout, no /tmp, no console spam)
-- @version 0.4.0

local reaper = reaper

-- ---------- portabler Helper-Pfad ----------
local function script_dir()
  local _, thisfn = reaper.get_action_context()
  return thisfn:match("^(.*)[/\\]") or "."
end
local function join(a,b)
  local sep = package.config:sub(1,1)
  if a:sub(-1) == "/" or a:sub(-1) == "\\" then return a..b end
  return a..sep..b
end
local function file_exists(p) local f=io.open(p,"r"); if f then f:close(); return true end end

local SCRIPT_DIR = script_dir()
local HELPER = join(SCRIPT_DIR, "reaper_portal_open.py")
if not file_exists(HELPER) then
  local res = reaper.GetResourcePath()
  local cand = {
    join(res, "Scripts/ReaperPortalFileChooser/reaper_portal_open.py"),
    join(res, "Scripts/reaper_portal_open.py")
  }
  for _,p in ipairs(cand) do if file_exists(p) then HELPER=p; break end end
end

-- Python-Binary (du hast gesagt 'python3' klappt bei dir)
local PY = "python3"

-- ---------- kleines JSON-Parsing für unser Format ----------
local function json_unescape(s)
  return (s:gsub('\\"','"'):gsub("\\\\","\\"):gsub("\\n","\n"):gsub("\\t","\t"):gsub("\\r","\r"))
end
local function parse_portal_json(txt)
  if not txt or txt == "" then return nil end
  local err = txt:match('"error"%s*:%s*"(.-)"'); if err then err = json_unescape(err) end
  local path = txt:match('"path"%s*:%s*"(.-)"'); if path then path = json_unescape(path) end
  local newtab = txt:match('"open_in_new_tab"%s*:%s*(true)') and true or false
  local fxoff  = txt:match('"fx_offline"%s*:%s*(true)') and true or false
  return { error = err, path = path, choices = { open_in_new_tab = newtab, fx_offline = fxoff } }
end

-- ---------- stdout lesen (ohne ExecProcess) ----------
local function run_and_read_stdout(cmd)
  local p = io.popen(cmd, "r")  -- blockiert bis Prozess beendet
  if not p then return nil, "popen failed" end
  local out = p:read("*a") or ""
  local ok, reason, code = p:close()  -- ok==true bei exit 0
  return out, (ok and nil or (reason..":"..tostring(code)))
end

local function quote(s) return string.format("%q", s) end

local function main()
  if not file_exists(HELPER) then
    -- NICHT in die Konsole schreiben, um das Fenster nicht zu öffnen:
    reaper.MB("Portal-Helper nicht gefunden:\n"..tostring(HELPER), "ReaScript", 0)
    return
  end

  -- Python: schreibe JSON auf stdout, Fehler auf stderr
  -- Wichtig: 2>&1 damit wir im Fehlerfall die Meldung im selben Stream sehen
  local cmd = string.format('%s -u %s --out - --err - 2>&1', quote(PY), quote(HELPER))
  local stdout, err = run_and_read_stdout(cmd)

  -- Abbrechen: Der Helper liefert dann JSON mit "path": null – das behandeln wir leise
  local obj = parse_portal_json(stdout or "")
  if not obj then
    -- Falls der Helper aus irgendeinem Grund nichts Sinnvolles zurückgab:
    -- komplett still bleiben? -> ja, außer du willst Debug:
    -- reaper.MB("Portal-Helper: keine gültige Antwort.", "ReaScript", 0)
    return
  end
  if obj.error then
    -- echte Fehler optional anzeigen
    reaper.MB("Portal-Fehler:\n"..tostring(obj.error), "ReaScript", 0)
    return
  end
  if not obj.path or obj.path == "" then
    -- Dialog abgebrochen -> komplett still
    return
  end

  -- Erfolgsfall: direkt öffnen (ohne irgendeinen Console-Output)
  reaper.Main_openProject(obj.path)
end

main()
