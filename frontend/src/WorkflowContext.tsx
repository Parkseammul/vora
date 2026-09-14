import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api } from "./api";
import { activeStep } from "./workflow";
import type { NodeKey, Workflow } from "./types";

interface WorkflowContextValue { workflowExecutionId: number; workflow: Workflow | null; currentStep: NodeKey | null; refetch: () => Promise<void>; }
const WorkflowContext = createContext<WorkflowContextValue | null>(null);

export function WorkflowProvider({ workflowExecutionId, children }: { workflowExecutionId: number; children: ReactNode }) {
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const refetch = useCallback(async () => setWorkflow(await api.getWorkflow(workflowExecutionId)), [workflowExecutionId]);
  useEffect(() => { void api.getWorkflow(workflowExecutionId).then(setWorkflow); }, [workflowExecutionId]);
  useEffect(() => {
    if (!workflow || !Object.values(workflow.nodes).some((node) => node?.status === "PENDING" || node?.status === "RUNNING")) return undefined;
    const timer = window.setInterval(() => { void refetch(); }, 2000);
    return () => window.clearInterval(timer);
  }, [refetch, workflow]);
  return <WorkflowContext.Provider value={{ workflowExecutionId, workflow, currentStep: workflow ? activeStep(workflow) : null, refetch }}>{children}</WorkflowContext.Provider>;
}

export function useWorkflow() { const value = useContext(WorkflowContext); if (!value) throw new Error("WorkflowProvider is required"); return value; }
