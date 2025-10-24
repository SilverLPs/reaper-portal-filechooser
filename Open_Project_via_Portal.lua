-- @description Open Project via native portal (Gio-only, stdout, silent)
-- @version 0.6.0

local reaper = reaper

-- Helper neben diesem Lua-Script oder im ResourcePath suchen
local function script_dir() local _,p=reaper.get_action_context(); return p:match("^(.*)[/\\]") or "." end
local function join(a,b) local s=package.config:sub(1,1); return a:sub(-1)==s and (a..b) or (a..s..b) end
local function exists(p) local f=io.open(p,"r"); if f then f:close(); return true end end
local function q(s) return string.format("%q", s) end

local PY = "python3"
local HELPER = join(script_dir(), "reaper_portal_open.py")
if not exists(HELPER) then
  local res = reaper.GetResourcePath()
  local cands = {
    join(res, "Scripts/ReaperPortalFileChooser/reaper_portal_open.py"),
    join(res, "Scripts/reaper_portal_open.py"),
  }
  for _,p in ipairs(cands) do if exists(p) then HELPER=p; break end end
end
if not exists(HELPER) then
  reaper.MB("Portal-Helper nicht gefunden.", "ReaScript", 0); return
end

local function json_unescape(s)
  return (s:gsub('\\"','"'):gsub("\\\\","\\"):gsub("\\n","\n"):gsub("\\t","\t"):gsub("\\r","\r"))
end
local function parse_json(txt)
  if not txt or txt=="" then return nil end
  local err = txt:match('"error"%s*:%s*"(.-)"'); if err then err=json_unescape(err) end
  local path= txt:match('"path"%s*:%s*"(.-)"'); if path then path=json_unescape(path) end
  local newtab = txt:match('"open_in_new_tab"%s*:%s*(true)') and true or false
  local fxoff  = txt:match('"fx_offline"%s*:%s*(true)') and true or false
  return {error=err, path=path, choices={open_in_new_tab=newtab, fx_offline=fxoff}}
end

local function run_and_read_stdout(cmd)
  local p = io.popen(cmd, "r"); if not p then return nil end
  local out = p:read("*a") or ""; p:close(); return out
end

local function main()
  -- JSON direkt von stdout lesen, Fehler nach stderr (werden ignoriert)
  local cmd = string.format('%s -u %s --out - --err - 2>&1', q(PY), q(HELPER))
  local stdout = run_and_read_stdout(cmd) or ""
  local obj = parse_json(stdout)
  if not obj or obj.error or not obj.path or obj.path=="" then
    -- still: Abbruch oder Fehler → einfach nichts tun
    return
  end
  reaper.Main_openProject(obj.path)
end

main()
