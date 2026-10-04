-- workload.lua: Multi-Action Load Harness for wrk
-- Emulates 1,000 distinct concurrent users reading, creating, updating, and deleting notes.
--
-- Distribution:
--   60% Read Notes List       GET    /v1/users/{user_id}/notes
--   20% Read Single Note      GET    /v1/notes/{note_id}
--   10% Create Note           POST   /v1/notes
--    7% Update Note           PUT    /v1/notes/{note_id}
--    3% Delete Note           DELETE /v1/notes/{note_id}

wrk.method = "GET"
local counter = 0

-- Initialize random seed per thread
function setup(thread)
    thread:set("id", counter)
end

request = function()
    counter = counter + 1
    local user_id = math.random(1, 1000)
    local note_id = math.random(1, 100000)
    local roll = math.random(1, 100)

    -- 60% Read Notes List (checks own notes)
    if roll <= 60 then
        return wrk.format("GET", "/v1/users/" .. user_id .. "/notes")

    -- 20% Read Single Note with Details
    elseif roll <= 80 then
        return wrk.format("GET", "/v1/notes/" .. note_id)

    -- 10% Create Note
    elseif roll <= 90 then
        local body = string.format('{"title":"Dynamic Note %d","content":"Load test simulated payload content","user_id":%d,"status":"active"}', counter, user_id)
        local headers = {["Content-Type"] = "application/json"}
        return wrk.format("POST", "/v1/notes", headers, body)

    -- 7% Update Note
    elseif roll <= 97 then
        local body = string.format('{"title":"Updated Note %d","content":"New updated content payload"}', counter)
        local headers = {["Content-Type"] = "application/json"}
        return wrk.format("PUT", "/v1/notes/" .. note_id, headers, body)

    -- 3% Delete Note
    else
        return wrk.format("DELETE", "/v1/notes/" .. note_id)
    end
end
