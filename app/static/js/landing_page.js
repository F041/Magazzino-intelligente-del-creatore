        document.addEventListener('DOMContentLoaded', function() {
            const tabs = document.querySelectorAll('.tab');
            const useCaseItems = document.querySelectorAll('.use-case-item'); // Seleziona tutti gli item
            
            // Funzione per mostrare il contenuto del tab
            function showTabContent(targetId) {
                useCaseItems.forEach(item => {
                    if (item.id === targetId) {
                        item.style.display = 'flex'; // O 'block' se il layout interno è diverso
                        item.classList.add('active');
                    } else {
                        item.style.display = 'none';
                        item.classList.remove('active');
                    }
                });
            }

            tabs.forEach(tab => {
                tab.addEventListener('click', function() {
                    tabs.forEach(t => t.classList.remove('active'));
                    this.classList.add('active');
                    const targetContentId = this.getAttribute('data-target');
                    showTabContent(targetContentId);
                });
            });
            
            // Mostra il contenuto del primo tab (creator) di default
            if (tabs.length > 0) {
                 showTabContent(tabs[0].getAttribute('data-target'));
            }


            // Smooth scrolling per anchor links (già presente, ottimo)
            document.querySelectorAll('a[href^="#"]').forEach(anchor => {
                anchor.addEventListener('click', function(e) {
                    e.preventDefault();
                    const targetEl = document.querySelector(this.getAttribute('href'));
                    if (targetEl) {
                        // Calcola la posizione del target e sottrai l'altezza dell'header sticky
                        const headerOffset = document.querySelector('header') ? document.querySelector('header').offsetHeight : 0;
                        const elementPosition = targetEl.getBoundingClientRect().top + window.pageYOffset;
                        const offsetPosition = elementPosition - headerOffset - 20; // -20 per un po' di padding extra

                        window.scrollTo({
                            top: offsetPosition,
                            behavior: 'smooth'
                        });
                    }
                });
            });
            
            // Animazione per feature cards 
            // Potrebbe essere migliorata usando Intersection Observer per attivarla quando entrano in viewport
            const featureCards = document.querySelectorAll('.feature-card');
            const observerOptions = {
                root: null, // rispetto alla viewport
                rootMargin: '0px',
                threshold: 0.1 // trigger quando il 10% dell'elemento è visibile
            };

            const observer = new IntersectionObserver((entries, observerInstance) => {
                entries.forEach(entry => {
                    if (entry.isIntersecting) {
                        const card = entry.target;
                        // Applica l'animazione
                        card.style.opacity = '1';
                        card.style.transform = 'translateY(0)';
                        observerInstance.unobserve(card); // Smetti di osservare dopo l'animazione
                    }
                });
            }, observerOptions);

            featureCards.forEach((card, index) => {
                // Imposta stato iniziale per animazione
                card.style.opacity = '0';
                card.style.transform = 'translateY(30px)';
                card.style.transition = 'opacity 0.5s ease-out, transform 0.5s ease-out';
                card.style.transitionDelay = `${index * 0.1}s`; // Delay progressivo
                observer.observe(card); // Inizia ad osservare la card
            });
        });