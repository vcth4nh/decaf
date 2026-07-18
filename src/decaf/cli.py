import typer

app = typer.Typer(add_completion=False)


@app.command()
def main() -> None:
    """decaf — all-in-one Java decompiler (implementation in progress)."""
    typer.echo("decaf: not implemented yet")
    raise typer.Exit(code=2)
