"use client"

import { useEffect, useState } from "react"
import { useEventFeedContext } from "@/providers/event-feed-stream"
import { useWorkflow } from "@/providers/workflow"
import { FileJson, FileType, Sheet } from "lucide-react"
import { useFormContext } from "react-hook-form"

import { Action, ActionType } from "@/types/schemas"
import { cn, getActionKey } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import {
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { ConfirmationDialog } from "@/components/confirmation-dialog"
import { type WorkflowControlsForm } from "@/components/console/console"
import { tileIconMapping } from "@/components/workspace/canvas/action-node"

export const supportedInputTypes = [
  {
    mimeType: "text/plain",
    icon: FileType,
    description: "Plain Text",
    disabled: true,
  },
  {
    mimeType: "application/json",
    icon: FileJson,
    description: "JavaScript Object Notation",
  },
  {
    mimeType: "text/csv",
    icon: Sheet,
    description: "Comma-Separated Value Files",
    disabled: true,
  },
]

export function ConsolePanel({
  className,
}: React.HTMLAttributes<HTMLDivElement>) {
  const { control } = useFormContext<WorkflowControlsForm>()
  const { workflow } = useWorkflow()
  const [actions, setActions] = useState<Action[]>([])
  const { isStreaming, clearEvents } = useEventFeedContext()
  useEffect(() => {
    if (workflow?.actions) {
      setActions(
        Object.values(workflow.actions).filter(
          (action) => action.type === "webhook"
        )
      )
    }
  }, [workflow?.actions])

  const handleClearEvents = () => {
    clearEvents()
    console.log("Cleared events")
  }

  return (
    <div
      className={cn(
        "relative hidden h-full flex-col items-start gap-2 md:flex",
        className
      )}
    >
      <Card className="flex size-full flex-col space-y-4 rounded-lg border p-4 shadow-sm">
        <div className="space-y-3">
          <div className="flex justify-between">
            <legend className="-ml-1 px-1 text-sm font-semibold">
              Run Configuration
            </legend>
            <Badge variant="outline" className="flex gap-2">
              <span
                className={cn(
                  "flex h-2 w-2 rounded-full",
                  isStreaming ? "bg-green-400" : "bg-gray-400"
                )}
              />
              <span>Status</span>
            </Badge>
          </div>
          <FormField
            control={control}
            name="actionKey"
            render={({ field }) => (
              <FormItem>
                <FormLabel htmlFor="actionKey">Entrypoint</FormLabel>
                <FormControl>
                  <Select
                    onValueChange={field.onChange}
                    defaultValue={field.value}
                  >
                    <SelectTrigger
                      id="actionKey"
                      className="items-start [&_[data-description]]:hidden"
                    >
                      <SelectValue placeholder="Select a webhook" />
                    </SelectTrigger>
                    <SelectContent>
                      {actions?.map((action, idx) => {
                        const { title, type, description } = action
                        const Icon = tileIconMapping[type as ActionType]
                        const actionKey = getActionKey(action)
                        return (
                          <SelectItem key={idx} value={actionKey}>
                            <div className="flex items-center gap-3 text-muted-foreground">
                              {Icon && <Icon className="size-5" />}
                              <div className="grid gap-0.5">
                                <span className="font-medium text-foreground">
                                  {title}
                                </span>
                                <p className="text-xs" data-description>
                                  {description || "No description available."}
                                </p>
                              </div>
                            </div>
                          </SelectItem>
                        )
                      })}
                    </SelectContent>
                  </Select>
                </FormControl>
                <FormMessage className="p-3" />
              </FormItem>
            )}
          />
        </div>
        <div className="space-y-3">
          <FormField
            control={control}
            name="mimeType"
            render={({ field }) => (
              <FormItem>
                <FormLabel htmlFor="mimeType">Input Type</FormLabel>
                <FormControl>
                  <Select
                    onValueChange={field.onChange}
                    defaultValue={field.value}
                  >
                    <SelectTrigger
                      id="mimeType"
                      className="items-start [&_[data-description]]:hidden"
                    >
                      <SelectValue placeholder="Select an input data type" />
                    </SelectTrigger>
                    <SelectContent>
                      {supportedInputTypes?.map(
                        (
                          { mimeType, icon: Icon, description, disabled },
                          idx
                        ) => {
                          return (
                            <SelectItem
                              key={idx}
                              value={mimeType}
                              disabled={disabled}
                            >
                              <div className="flex items-center gap-3 text-muted-foreground">
                                {Icon && <Icon className="size-5" />}
                                <div className="grid gap-0.5">
                                  <span className="font-medium text-foreground">
                                    {mimeType}
                                  </span>
                                  <p className="text-xs" data-description>
                                    {description || "No description available."}
                                  </p>
                                </div>
                              </div>
                            </SelectItem>
                          )
                        }
                      )}
                    </SelectContent>
                  </Select>
                </FormControl>
                <FormMessage className="p-3" />
              </FormItem>
            )}
          />
        </div>
        <div className="flex flex-1 items-end">
          <ConfirmationDialog
            title="Are you sure?"
            description="You are about to clear all console events from your local browser storage. This action cannot be undone."
            onConfirm={handleClearEvents}
          >
            <Button className={cn("w-full")} type="button" variant="outline">
              Clear Events
            </Button>
          </ConfirmationDialog>
        </div>
      </Card>
    </div>
  )
}
