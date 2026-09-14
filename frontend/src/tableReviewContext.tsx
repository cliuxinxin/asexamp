import {createContext} from 'react';

export type TableReviewRequest={artifactId:string;revision?:number;runId?:string;proposalId?:string;readOnly?:boolean;promptId?:string};

// All review launchers use the same workspace owned by the active chat.
export const TableReviewContext=createContext<{open?:(request:TableReviewRequest)=>void}>({});
