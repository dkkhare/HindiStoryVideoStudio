"""Hosted ComfyUI API client for kImage."""
import asyncio, json, uuid
from pathlib import Path
import httpx

class KImageComfyAPI:
    def __init__(self, api_url: str, api_key: str = "", workflow_json: str = ""):
        self.base_url=(api_url or "").rstrip("/")
        self.api_key=api_key or ""
        if not self.base_url: raise ValueError("Configure the kImage ComfyUI API URL in Project API Settings.")
        try: self.workflow=json.loads(workflow_json) if isinstance(workflow_json,str) else workflow_json
        except json.JSONDecodeError as exc: raise ValueError("kImage workflow JSON is invalid.") from exc
        if not isinstance(self.workflow,dict) or not self.workflow: raise ValueError("Paste the API-exported mandirImageKAPI.json workflow in Project API Settings.")
        self.headers={"Authorization":f"Bearer {self.api_key}"} if self.api_key else {}

    async def _upload(self, client, path):
        path=Path(path)
        with path.open("rb") as fh:
            r=await client.post(f"{self.base_url}/upload/image",headers=self.headers,data={"overwrite":"true","type":"input"},files={"image":(path.name,fh,"image/png")})
        r.raise_for_status(); data=r.json(); name=data.get("name") or data.get("filename")
        if not name: raise RuntimeError(f"kImage upload returned no filename: {data}")
        sub=data.get("subfolder") or ""; return f"{sub}/{name}" if sub else name

    @staticmethod
    def _dimensions(ratio,size):
        base=1024 if str(size).upper()=="1K" else 1536
        try:
            a,b=(float(x) for x in ratio.split(":")); scale=base/max(a,b)
            return max(64,int(a*scale//64)*64),max(64,int(b*scale//64)*64)
        except Exception: return 1280,720

    def _patch(self,prompt,refs,width,height):
        wf=json.loads(json.dumps(self.workflow)); prompts=[]; loaders=[]; save=[]
        for nid,node in wf.items():
            if not isinstance(node,dict): continue
            cls=str(node.get("class_type","")).lower(); inputs=node.setdefault("inputs",{})
            for key,val in list(inputs.items()):
                k=str(key).lower()
                if isinstance(val,str) and k in {"prompt","text","positive_prompt","positive"}: prompts.append((nid,key))
                if k in {"width","w"} and isinstance(val,(int,float)): inputs[key]=width
                if k in {"height","h"} and isinstance(val,(int,float)): inputs[key]=height
            if "loadimage" in cls or cls in {"load_image","imageinput"}: loaders.append(inputs)
            if "saveimage" in cls or "previewimage" in cls: save.append(nid)
        if not prompts: raise RuntimeError("mandirImageKAPI.json has no recognizable prompt/text input node.")
        nid,key=prompts[0]; wf[nid]["inputs"][key]=prompt
        if refs and not loaders: raise RuntimeError("kImage workflow has no LoadImage node for reference images.")
        if len(refs)>len(loaders): raise RuntimeError(f"kImage workflow has {len(loaders)} reference inputs; this scene needs {len(refs)}.")
        for inputs,name in zip(loaders,refs):
            key="image" if "image" in inputs else next((k for k in inputs if "image" in k.lower()),None)
            if key: inputs[key]=name
        if not save: raise RuntimeError("mandirImageKAPI.json has no SaveImage or PreviewImage output node.")
        return wf

    async def generate_image(self,prompt,output_path,reference_images,aspect_ratio="16:9",image_size="1K"):
        async with httpx.AsyncClient(timeout=httpx.Timeout(1800,connect=60)) as client:
            refs=[await self._upload(client,p) for p in reference_images]
            w,h=self._dimensions(aspect_ratio,image_size); wf=self._patch(prompt,refs,w,h)
            r=await client.post(f"{self.base_url}/prompt",headers=self.headers,json={"prompt":wf,"client_id":str(uuid.uuid4())})
            r.raise_for_status(); pid=r.json().get("prompt_id")
            if not pid: raise RuntimeError(f"kImage /prompt returned no prompt_id: {r.text[:400]}")
            deadline=asyncio.get_running_loop().time()+1800; outputs={}
            while asyncio.get_running_loop().time()<deadline:
                await asyncio.sleep(2); poll=await client.get(f"{self.base_url}/history/{pid}",headers=self.headers)
                poll.raise_for_status(); hist=poll.json().get(pid,{})
                outputs=hist.get("outputs") or {}; status=hist.get("status") or {}
                if outputs: break
                if isinstance(status,dict) and status.get("status_str")=="error": raise RuntimeError(f"kImage execution failed: {status}")
            else: raise TimeoutError(f"kImage workflow {pid} timed out after 30 minutes.")
            images=[im for node in outputs.values() if isinstance(node,dict) for im in node.get("images",[])]
            if not images: raise RuntimeError(f"kImage workflow {pid} completed without image output.")
            im=images[0]; result=await client.get(f"{self.base_url}/view",headers=self.headers,params={"filename":im["filename"],"subfolder":im.get("subfolder",""),"type":im.get("type","output")})
            result.raise_for_status(); out=Path(output_path); out.parent.mkdir(parents=True,exist_ok=True)
            tmp=out.with_suffix(out.suffix+".tmp"); tmp.write_bytes(result.content); tmp.replace(out)
        return {"model":"kImage (ComfyUI)","prompt_id":pid}
