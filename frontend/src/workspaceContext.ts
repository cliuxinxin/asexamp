import {createContext} from 'react';

export type ArtifactWorkspaceRequest={artifactId:string;revision?:number;runId?:string;proposalId?:string;readOnly?:boolean;promptId?:string};

export const ArtifactWorkspaceContext=createContext<{open?:(request:ArtifactWorkspaceRequest)=>void}>({});
