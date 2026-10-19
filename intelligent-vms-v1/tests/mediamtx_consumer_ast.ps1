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
        if ($parent -is [Management.Automation.Language.FunctionDefinitionAst]) {
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

function Get-AssignmentVariable($Left) {
    # A type constraint wraps the target; it must not hide a conflicting write.
    while ($Left -is [Management.Automation.Language.ConvertExpressionAst] -or
        $Left -is [Management.Automation.Language.AttributedExpressionAst]) {
        $Left = $Left.Child
    }
    if ($Left -is [Management.Automation.Language.VariableExpressionAst]) { return $Left }
    return $null
}

function Get-ConstantCommandText($Expression) {
    # Resolve only literal command names and literal concatenation, never evaluate code.
    if ($Expression -is [Management.Automation.Language.StringConstantExpressionAst]) {
        return $Expression.Value
    }
    if ($Expression -is [Management.Automation.Language.ParenExpressionAst]) {
        return Get-ConstantCommandText (Get-PipelineExpression $Expression.Pipeline)
    }
    if ($Expression -is [Management.Automation.Language.BinaryExpressionAst] -and
        $Expression.Operator -eq "Plus") {
        $left = Get-ConstantCommandText $Expression.Left
        $right = Get-ConstantCommandText $Expression.Right
        if ($null -ne $left -and $null -ne $right) { return $left + $right }
    }
    return $null
}

function Test-LiteralThrow($Statement) {
    # A ThrowStatementAst can contain return/exit in its operand; its AST type alone
    # does not prove failure on checksum mismatch. Do not evaluate consumer source.
    if ($Statement -isnot [Management.Automation.Language.ThrowStatementAst]) {
        return $false
    }
    $message = Get-PipelineExpression $Statement.Pipeline
    if ($message -isnot [Management.Automation.Language.StringConstantExpressionAst]) {
        return $false
    }
    if ($message.StringConstantType -notin @(
        [Management.Automation.Language.StringConstantType]::SingleQuoted,
        [Management.Automation.Language.StringConstantType]::DoubleQuoted
    )) {
        return $false
    }
    return (-not [string]::IsNullOrWhiteSpace($message.Value))
}

function Test-IndirectVariableWrite($Node) {
    if ($Node -is [Management.Automation.Language.InvokeMemberExpressionAst]) {
        return $Node.Member.Extent.Text -imatch '^(Set|Remove)$' -and
            ($Node.Expression.Extent.Text -replace '\s', '') -imatch '\.SessionState\.PSVariable$'
    }
    if ($Node -isnot [Management.Automation.Language.CommandAst]) { return $false }
    $command = (Get-ConstantCommandText $Node.CommandElements[0]) -split '\\'
    $name = $command[-1]
    if ($name -in @('Set-Variable', 'New-Variable', 'Clear-Variable', 'Remove-Variable',
        'sv', 'set', 'nv', 'clv', 'rv', 'Invoke-Expression', 'iex')) { return $true }
    if ($name -in @('Set-Item', 'New-Item', 'Remove-Item', 'Clear-Item', 'si', 'ni', 'ri', 'cli')) {
        foreach ($argument in $Node.CommandElements | Select-Object -Skip 1) {
            $text = Get-ConstantCommandText $argument
            if ($null -ne $text -and $text -imatch '^(Microsoft\.PowerShell\.Core\\)?Variable:') {
                return $true
            }
        }
    }
    return $false
}

# This is a bounded source contract, not an interpreter for arbitrary PowerShell.
# Rebound commands, computed provider paths and side effects in called functions
# still require independent review; only the explicit forms above are recognized.
# Reject nonlocal exits and trap handlers instead of inferring their conditions.
# Conditional preflight throws are legitimate and do not suppress a later guard.
$executableNodes = @($sourceAst.FindAll({ param($node)
    $node -is [Management.Automation.Language.AssignmentStatementAst] -or
    $node -is [Management.Automation.Language.IfStatementAst] -or
    $node -is [Management.Automation.Language.CommandAst] -or
    $node -is [Management.Automation.Language.InvokeMemberExpressionAst] -or
    $node -is [Management.Automation.Language.ReturnStatementAst] -or
    $node -is [Management.Automation.Language.ExitStatementAst] -or
    $node -is [Management.Automation.Language.BreakStatementAst] -or
    $node -is [Management.Automation.Language.ContinueStatementAst] -or
    $node -is [Management.Automation.Language.TrapStatementAst]
}, $true) |
    Where-Object { Test-ExecutableScope $_ })
$flowExits = @($executableNodes | Where-Object {
    $_ -is [Management.Automation.Language.ReturnStatementAst] -or
    $_ -is [Management.Automation.Language.ExitStatementAst] -or
    $_ -is [Management.Automation.Language.BreakStatementAst] -or
    $_ -is [Management.Automation.Language.ContinueStatementAst]
})
$hasTrap = @($executableNodes | Where-Object {
    $_ -is [Management.Automation.Language.TrapStatementAst]
}).Count -gt 0

$references = @($executableNodes | Where-Object {
    $node = $_
    $node -is [Management.Automation.Language.AssignmentStatementAst] -or
    $node -is [Management.Automation.Language.IfStatementAst] -or
    $node -is [Management.Automation.Language.CommandAst] -or
    $node -is [Management.Automation.Language.InvokeMemberExpressionAst]
} | ForEach-Object {
    $node = $_
    if (Test-ExecutableScope $node) {
        $literal = $null
        $direct = $node.Parent -eq $sourceAst.EndBlock
        $earlierFlowExits = @($flowExits | Where-Object {
            $_.Extent.StartOffset -lt $node.Extent.StartOffset
        })
        $direct = $direct -and $earlierFlowExits.Count -eq 0
        # A direct node after an unconditional script exit is still dead code.
        if ($direct) {
            $earlierExits = @($sourceAst.EndBlock.Statements | Where-Object {
                $_.Extent.StartOffset -lt $node.Extent.StartOffset -and (
                    $_ -is [Management.Automation.Language.ReturnStatementAst] -or
                    $_ -is [Management.Automation.Language.ExitStatementAst] -or
                    $_ -is [Management.Automation.Language.ThrowStatementAst] -or
                    $_ -is [Management.Automation.Language.BreakStatementAst] -or
                    $_ -is [Management.Automation.Language.ContinueStatementAst])
            })
            $direct = $earlierExits.Count -eq 0
        }
        $matchesConsumer = $false
        $variable = $null
        if ($node -is [Management.Automation.Language.AssignmentStatementAst]) {
            $variable = Get-AssignmentVariable $node.Left
        }
        if ($request.kind -eq "assignment" -and
            $null -ne $variable -and
            ($variable.VariablePath.UserPath -split ':')[-1] -ieq $request.selector) {
            $matchesConsumer = $true
            if ($node.Operator -eq "Equals" -and
                $node.Left -is [Management.Automation.Language.VariableExpressionAst] -and
                $node.Left.VariablePath.UserPath -ieq $request.selector -and
                $node.Right -is [Management.Automation.Language.CommandExpressionAst]) {
                $literal = $node.Right.Expression
            }
        } elseif ($request.kind -eq "assignment" -and
            (Test-IndirectVariableWrite $node)) {
            # Variable-writing commands are outside the accepted literal-pin contract.
            $matchesConsumer = $true
        } elseif ($request.kind -eq "guard" -and
            $node -is [Management.Automation.Language.IfStatementAst]) {
            foreach ($clause in $node.Clauses) {
                $condition = Get-PipelineExpression $clause.Item1
                if ($condition -is [Management.Automation.Language.BinaryExpressionAst] -and
                    ($condition.Left.Extent.Text -replace '\s', '') -ieq
                    ($request.selector -replace '\s', '')) {
                    $matchesConsumer = $true
                    $body = $clause.Item2.Statements
                    $direct = $direct -and -not $hasTrap -and
                        $node.Clauses.Count -eq 1 -and $body.Count -eq 1 -and
                        (Test-LiteralThrow $body[0])
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
