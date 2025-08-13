from rich.console import Console
from rich.table import Table
import pyfiglet
import time
import os

console = Console()

def clear():
    os.system("cls" if os.name == "nt" else "clear")

def banner():
    console.print(pyfiglet.figlet_format("Insta Tool", font="slant"), style="bold cyan")
    console.print("📸 [bold green]Instagram Content Helper[/bold green]", justify="center")
    console.print("—"*50, style="dim")

def menu():
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("№", style="dim", width=5)
    table.add_column("Действие")
    table.add_row("1", "Запланировать пост")
    table.add_row("2", "Обработать фото в видео")
    table.add_row("3", "Выложить пост в Instagram")
    table.add_row("4", "Выйти")
    console.print(table)

def main():
    while True:
        clear()
        banner()
        menu()
        choice = console.input("\n[bold yellow]Выберите действие:[/bold yellow] ")
        
        if choice == "1":
            console.print("🗓 Запуск планировщика постов...", style="bold cyan")
            time.sleep(1)
        elif choice == "2":
            console.print("🎬 Обработка фото в видео...", style="bold cyan")
            time.sleep(1)
        elif choice == "3":
            console.print("🚀 Публикация в Instagram...", style="bold cyan")
            time.sleep(1)
        elif choice == "4":
            console.print("👋 Выход...", style="bold red")
            break
        else:
            console.print("[bold red]Неверный выбор![/bold red]")
            time.sleep(1)

if __name__ == "__main__":
    main()
