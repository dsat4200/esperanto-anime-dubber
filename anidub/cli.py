from rich.console import Console
from rich.panel import Panel

console = Console()


def main():
    console.print(
        Panel.fit(
            "[bold cyan]anidub[/] - anime dubbing pipeline\n"
            "[dim]Full pipeline coming. Use `anidub-test-voice` for now.[/]",
            border_style="cyan",
        )
    )


if __name__ == "__main__":
    main()