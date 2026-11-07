export const easternZone='America/New_York';
export const beijingZone='Asia/Shanghai';
export function offsetZone(hours:number){
  const minutes=Math.round(hours*60);
  return `UTC${minutes>=0?'+':'-'}${String(Math.floor(Math.abs(minutes)/60)).padStart(2,'0')}:${String(Math.abs(minutes)%60).padStart(2,'0')}`;
}
export function offsetMinutes(zone:string){
  const match=/^UTC([+-])(\d{2}):(\d{2})$/.exec(zone);
  return match?(Number(match[2])*60+Number(match[3]))*(match[1]==='+'?1:-1):null;
}
export function validZone(zone:string){
  if(zone===easternZone||zone===beijingZone)return true;
  const minutes=offsetMinutes(zone);
  return minutes!=null&&minutes>=-720&&minutes<=840&&minutes%15===0&&zone===offsetZone(minutes/60);
}
export function readZone(){
  try{const zone=localStorage.getItem('backtest-time-zone')||sessionStorage.getItem('backtest-time-zone')||easternZone;return validZone(zone)?zone:easternZone;}catch{return easternZone;}
}
export function zoneLabel(zone:string,short=false){
  if(zone===easternZone)return short?'美东 ET':'美东 ET（冬令 UTC−05:00 / 夏令 UTC−04:00，自动切换）';
  if(zone===beijingZone)return short?'UTC+08:00':'北京时间（UTC+08:00）';
  return short?zone:zone+'（固定偏移，不切换夏令时）';
}
export function zonedStamp(ts:number,zone:string){
  const offset=offsetMinutes(zone);
  return offset==null?new Date(ts*1000).toLocaleString('zh-CN',{timeZone:zone,hour12:false,timeZoneName:'short'}):new Date((ts+offset*60)*1000).toLocaleString('zh-CN',{timeZone:'UTC',hour12:false})+' '+zone;
}
