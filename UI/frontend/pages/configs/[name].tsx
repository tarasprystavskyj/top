
import {useRouter} from 'next/router'; import useSWR from 'swr'; import dynamic from 'next/dynamic'; import {useState} from 'react';
import { apiFetch } from '../../utils/api';
const Monaco = dynamic(()=>import('@monaco-editor/react'),{ssr:false}); const f=(u:string)=>apiFetch(u).then(r=>r.json());
export default function P(){ const r=useRouter(); const name=r.query.name as string; const {data,mutate}=useSWR(name?'/api/configs/'+name:null,f); const [txt,setTxt]=useState<string>('');
 if(!data) return 'Loading...';
 return <div><h3>{name}</h3><div style={{height:500,border:'1px solid #eee'}}><Monaco language='yaml' value={txt||data.yaml_text} onChange={v=>setTxt(v||'')} options={{minimap:{enabled:false}}}/></div>
 <button onClick={async()=>{const res=await apiFetch('/api/configs/'+name,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({yaml_text:txt||data.yaml_text})}); alert(res.ok?'Saved':'Failed'); mutate();}}>Save</button></div>;
}
