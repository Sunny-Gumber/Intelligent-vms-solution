# Parse checksum consumers as data; never invoke the source being inspected.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$tokens = $null
$parseErrors = $null
$sourceAst = [Management.Automation.Language.Parser]::ParseInput(
    $request.source, [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count) { throw "Invalid PowerShell checksum consumer syntax" }

function Test-ExecutableScope($Node) {
    # Captured script blocks and function bodies are not top-level consumers.
    for ($parent = $Node.Parent; $parent; $parent = $parent.Parent) {
        if ($parent -is [Management.Automation.Language.ScriptBlockExpressionAst] -and
            $parent.Parent -isnot [Management.Automation.Language.CommandAst]) {
            return $false
        }
        if ($parent -is [Management.Automation.Language.FunctionDefinitionAst] -or
            $parent -is [Management.Automation.Language.ExpandableStringExpressionAst]) {
            return $false
        }
    }
    return $true
}

function Get-PipelineExpression($Pipeline) {
    if ($Pipeline -is [Management.Automation.Language.PipelineAst] -and
        $Pipeline.PipelineElements.Count -eq 1 -and
        $Pipeline.PipelineElements[0] -is [Management.Automation.Language.CommandExpressionAst]) {
        return $Pipeline.PipelineElements[0].Expression
    }
    return $null
}

$references = @($sourceAst.FindAll({ param($node)
    $node -is [Management.Automation.Language.AssignmentStatementAst] -or
    $node -is [Management.Automation.Language.IfStatementAst]
}, $true) | ForEach-Object {
    $node = $_
    if (Test-ExecutableScope $node) {
        $literal = $null
        $direct = $node.Parent -eq $sourceAst.EndBlock
        $matchesConsumer = $false
        if ($request.kind -eq "assignment" -and
            $node -is [Management.Automation.Language.AssignmentStatementAst] -and
            $node.Left -is [Management.Automation.Language.VariableExpressionAst] -and
            $node.Left.VariablePath.UserPath -ieq $request.selector) {
            $matchesConsumer = $true
            if ($node.Operator -eq "Equals" -and
                $node.Right -is [Management.Automation.Language.CommandExpressionAst]) {
                $literal = $node.Right.Expression
            }
        } elseif ($request.kind -eq "guard" -and
            $node -is [Management.Automation.Language.IfStatementAst]) {
            foreach ($clause in $node.Clauses) {
                $condition = Get-PipelineExpression $clause.Item1
                if ($condition -is [Management.Automation.Language.BinaryExpressionAst] -and
                    ($condition.Left.Extent.Text -replace '\s', '') -ieq
                    ($request.selector -replace '\s', '')) {
                    $matchesConsumer = $true
                    $throws = @($clause.Item2.Statements | Where-Object {
                        $_ -is [Management.Automation.Language.ThrowStatementAst]
                    })
                    $direct = $direct -and $node.Clauses.Count -eq 1 -and $throws.Count -gt 0
                    if ($condition.Operator -eq "Ine") { $literal = $condition.Right }
                }
            }
        }
        if ($matchesConsumer) {
            $value = $null
            if ($literal -is [Management.Automation.Language.StringConstantExpressionAst]) {
                $value = $literal.Value
            }
            [ordered]@{ value = $value; statement = $node.Extent.Text; direct = $direct; scope = "" }
        }
    }
})
ConvertTo-Json -InputObject $references -Depth 4 -Compress
