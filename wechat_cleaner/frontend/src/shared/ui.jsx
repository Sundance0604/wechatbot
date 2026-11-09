import React from "react";
import {Inbox} from "lucide-react";
export function Button({children,icon:Icon,variant='default',className='',...props}) {return <button className={`btn ${variant} ${className}`} {...props}>{Icon&&<Icon size={16}/>} {children}</button>}
export function Badge({children,tone='neutral'}) {return <span className={`badge ${tone}`}>{children}</span>}
export function Field({label,hint,children}) {return <label className="field"><span>{label}</span>{children}{hint&&<small>{hint}</small>}</label>}
export function Empty({icon:Icon=Inbox,title,description,children}) {return <div className="empty"><div className="empty-icon"><Icon size={27}/></div><h3>{title}</h3><p>{description}</p>{children}</div>}
