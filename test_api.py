import os, json, time
os.environ["DB_PATH"]="test.db"; os.environ["ADMIN_PASSWORD"]="pw"
from fastapi.testclient import TestClient
import app as A
c=TestClient(A.app)
assert c.get("/").status_code==200 and c.get("/api/config").json()["papers"][1]["count"]==100
r=c.post("/api/start",json={"name":"Asha","school":"CA","paper":1,"shuffle":True}).json()
assert "a" not in r["questions"][0]                      # answers never sent
assert c.post("/api/start",json={"name":"asha","paper":1}).status_code==409   # duplicate blocked
aid,tok=r["attempt_id"],r["token"]
# answer first 10 correctly, 5 wrongly via server key
ans={}
for i,q in enumerate(r["questions"][:15]):
    right=A.QBANK[q["id"]]["a"]; ans[q["id"]]=right if i<10 else next(l for l in "ABCD" if l!=right)
assert c.post("/api/answers",json={"attempt_id":aid,"token":tok,"answers":ans}).json()["saved"]==15
assert c.post("/api/answers",json={"attempt_id":aid,"token":"bad","answers":ans}).status_code==403
res=c.post("/api/resume",json={"attempt_id":aid,"token":tok}).json(); assert len(res["answers"])==15
out=c.post("/api/submit",json={"attempt_id":aid,"token":tok,"answers":{}}).json()
assert (out["correct"],out["wrong"],out["skipped"])==(10,5,85), out
assert len(out["review"])==100
assert c.post("/api/answers",json={"attempt_id":aid,"token":tok,"answers":ans}).status_code==409
# expiry: player 2 never submits
r2=c.post("/api/start",json={"name":"Ben","paper":0}).json()
q=r2["questions"][0]["id"]
c.post("/api/answers",json={"attempt_id":r2["attempt_id"],"token":r2["token"],"answers":{q:A.QBANK[q]["a"]}})
with A.db() as d: d.execute("UPDATE attempts SET deadline=? WHERE id=?",(time.time()-60,r2["attempt_id"]))
assert c.post("/api/answers",json={"attempt_id":r2["attempt_id"],"token":r2["token"],"answers":{q:"A"}}).status_code==410
out2=c.post("/api/resume",json={"attempt_id":r2["attempt_id"],"token":r2["token"]}).json()
assert out2["finished"] and out2["result"]["correct"]==1 and out2["result"]["auto_submitted"]
# admin
assert c.get("/api/admin/overview").status_code==401
a=("admin","pw"); ov=c.get("/api/admin/overview",auth=a).json(); assert len(ov["attempts"])==2
assert c.get("/api/leaderboard?paper=1").status_code==403
c.post("/api/admin/settings",json={"public_leaderboard":True,"access_code":"X1"},auth=a)
assert c.get("/api/leaderboard?paper=1").json()[0]["score"]==10
assert c.post("/api/start",json={"name":"Cy","paper":0}).status_code==403
assert c.post("/api/start",json={"name":"Cy","paper":0,"access_code":"X1"}).status_code==200
assert "Asha" in c.get("/api/admin/export.csv",auth=a).text
print("ALL TESTS PASSED")
